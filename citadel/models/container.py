# -*- coding: utf-8 -*-

from datetime import timedelta, datetime
from numbers import Number
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from time import sleep

from citadel.config import CORE_DEPLOY_INFO_PATH
from citadel.ext import db
from citadel.libs.datastructure import purge_none_val_from_dict
from citadel.libs.utils import logger
from citadel.models.base import BaseModelMixin
from citadel.rpc.client import get_core


class ContainerOverrideStatus:
    NONE = 0
    DEBUG = 1
    REMOVING = 2


class Container(BaseModelMixin):
    __table_args__ = (
        db.Index('appname_sha', 'appname', 'sha'),
    )

    appname = db.Column(db.CHAR(64), nullable=False)
    sha = db.Column(db.CHAR(64), nullable=False)
    container_id = db.Column(db.CHAR(64), nullable=False, index=True)
    container_name = db.Column(db.CHAR(64), nullable=False, index=True)
    combo_name = db.Column(db.CHAR(64), nullable=False)
    entrypoint_name = db.Column(db.String(50), nullable=False)
    envname = db.Column(db.String(50))
    cpu_quota = db.Column(db.Numeric(12, 3), nullable=False)
    memory = db.Column(db.BigInteger, nullable=False)
    zone = db.Column(db.String(50), nullable=False)
    podname = db.Column(db.String(50), nullable=False)
    nodename = db.Column(db.String(50), nullable=False)
    deploy_info = db.Column(db.JSON, default={})
    override_status = db.Column(db.Integer, default=ContainerOverrideStatus.NONE)
    initialized = db.Column(db.Integer, default=0)

    def __str__(self):
        return '<Container {c.zone}:{c.appname}:{c.short_sha}:{c.entrypoint_name}:{c.short_id}>'.format(c=self)

    @classmethod
    def create(cls, appname=None, sha=None, container_id=None,
               container_name=None, combo_name=None, entrypoint_name=None,
               envname=None, cpu_quota=None, memory=None, zone=None,
               podname=None, nodename=None,
               override_status=ContainerOverrideStatus.NONE):
        try:
            c = cls(appname=appname, sha=sha, container_id=container_id,
                    container_name=container_name, combo_name=combo_name,
                    entrypoint_name=entrypoint_name, envname=envname,
                    cpu_quota=cpu_quota, memory=memory, zone=zone,
                    podname=podname, nodename=nodename,
                    override_status=override_status)
            db.session.add(c)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # TODO: This must not go wrong!
            raise

        return c

    @classmethod
    def get_by_container_id(cls, container_id):
        """get by container_id, prefix can be used in container_id"""
        if len(container_id or '') < 7:
            raise ValueError('Must provide full container ID, got {}'.format(container_id))
        c = cls.query.filter(cls.container_id.like('{}%'.format(container_id))).first()
        return c

    @classmethod
    def get_by_container_ids(cls, container_ids):
        containers = [cls.get_by_container_id(cid) for cid in container_ids]
        return [c for c in containers if c]

    @property
    def core_deploy_key(self):
        return '{prefix}/{c.appname}/{c.entrypoint_name}/{c.nodename}/{c.container_id}'.format(c=self, prefix=CORE_DEPLOY_INFO_PATH)

    def is_healthy(self):
        return self.deploy_info.get('Healthy')

    @property
    def app(self):
        from .app import App
        return App.get_by_name(self.appname)

    @property
    def release(self):
        from .app import Release
        return Release.get_by_app_and_sha(self.appname, self.sha)

    @classmethod
    def get_by(cls, **kwargs):
        sha = kwargs.pop('sha', '')
        container_id = kwargs.pop('container_id', '')

        query_set = cls.query.filter_by(**purge_none_val_from_dict(kwargs))

        if sha:
            query_set = query_set.filter(cls.sha.like('{}%'.format(sha)))

        if container_id:
            query_set = query_set.filter(cls.container_id.like('{}%'.format(container_id)))

        return query_set.order_by(cls.id.desc()).all()

    @property
    def specs_entrypoint(self):
        return self.release.specs.entrypoints[self.entrypoint_name]

    @property
    def backup_path(self):
        return self.specs_entrypoint.backup_path

    @property
    def publish(self):
        return self.deploy_info.get('Publish', {})

    @property
    def ident(self):
        return self.container_name.rsplit('_', 2)[-1]

    @property
    def short_id(self):
        return self.container_id[:7]

    @property
    def short_sha(self):
        return self.sha[:7]

    def is_removing(self):
        return self.override_status == ContainerOverrideStatus.REMOVING

    def is_cronjob(self):
        return self.entrypoint_name in self.app.cronjob_entrypoints

    def is_debug(self):
        return self.override_status == ContainerOverrideStatus.DEBUG

    def mark_debug(self):
        self.override_status = ContainerOverrideStatus.DEBUG
        try:
            db.session.add(self)
            db.session.commit()
        except StaleDataError:
            db.session.rollback()

    def mark_removing(self):
        self.override_status = ContainerOverrideStatus.REMOVING
        try:
            db.session.add(self)
            db.session.commit()
        except StaleDataError:
            db.session.rollback()

    def mark_initialized(self):
        self.initialized = 1
        db.session.add(self)
        db.session.commit()

    def update_deploy_info(self, deploy_info):
        logger.debug('Update deploy_info for %s: %s', self, deploy_info)
        self.deploy_info = deploy_info
        db.session.add(self)
        db.session.commit()

    def wait_for_erection(self, timeout=None):
        """wait until this container is healthy, timeout can be timedelta or
        seconds, timeout default to erection_timeout in specs, if timeout is 0,
        don't even wait and just report healthy"""
        if not timeout:
            timeout = timedelta(seconds=self.release.specs.erection_timeout)
        elif isinstance(timeout, Number):
            timeout = timedelta(seconds=timeout)

        if not timeout:
            return True

        must_end = datetime.now() + timeout
        logger.debug('Waiting for container %s to become healthy...', self)
        while datetime.now() < must_end:
            if self.is_healthy():
                return True
            sleep(2)
            # deploy_info is written by watch-etcd services, so it's very
            # important to constantly query database, without refresh we'll be
            # constantly hitting sqlalchemy cache
            db.session.refresh(self, attribute_names=['deploy_info'])
            db.session.commit()

        return False

    def status(self):
        if self.is_debug():
            return 'debug'
        if self.is_removing():
            return 'removing'
        running = self.deploy_info.get('Running')
        healthy = self.deploy_info.get('Healthy')
        if running:
            if healthy:
                return 'running'
            else:
                return 'sick'
        else:
            return 'dead'

    def get_node(self):
        return get_core(self.zone).get_node(self.podname, self.nodename)
