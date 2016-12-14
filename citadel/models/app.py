# -*- coding: utf-8 -*-
from sqlalchemy import event, DDL
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import cached_property

from citadel.ext import db, gitlab
from citadel.libs.utils import logger
from citadel.models.base import BaseModelMixin, ModelDeleteError
from citadel.models.gitlab import get_project_name, get_file_content, get_commit
from citadel.models.loadbalance import ELBRule
from citadel.models.specs import Specs
from citadel.models.user import User


class App(BaseModelMixin):
    __tablename__ = 'app'
    name = db.Column(db.CHAR(64), nullable=False, unique=True)
    # 这货就是 git@gitlab.ricebook.net...
    git = db.Column(db.String(255), nullable=False)
    user_id = db.Column(db.Integer, nullable=False, default=0)

    @classmethod
    def get_or_create(cls, name, git):
        app = cls.get_by_name(name)
        if app:
            return app

        try:
            app = cls(name=name, git=git)
            db.session.add(app)
            db.session.commit()
            return app
        except IntegrityError:
            db.session.rollback()
            return None

    @classmethod
    def get_by_name(cls, name):
        return cls.query.filter_by(name=name).first()

    @classmethod
    def get_by_user(cls, user_id, start=0, limit=20):
        """拿这个user可以有的app, 跟app自己的user_id没关系."""
        names = AppUserRelation.get_appname_by_user_id(user_id, start, limit)
        return [cls.get_by_name(n) for n in names]

    @property
    def uid(self):
        """用来修正app的uid, 默认使用id"""
        return self.user_id or self.id

    @property
    def project_name(self):
        return get_project_name(self.git)

    @property
    def container_list(self):
        from .container import Container
        return Container.get_by_app(self.name)

    @property
    def has_problematic_container(self):
        containers = self.container_list
        if not containers or {c.status() for c in containers} == {'running'}:
            return False
        else:
            return True

    @property
    def gitlab_project(self):
        gitlab_project_name = get_project_name(self.git)
        return gitlab.projects.get(gitlab_project_name)

    def get_release(self, sha):
        return Release.get_by_app_and_sha(self.name, sha)

    def delete(self):
        appname = self.name
        from .loadbalance import ELBRule
        containers = self.container_list
        if containers:
            raise ModelDeleteError('App {} got containers {}, remove them before deleting app'.format(appname, containers))
        # delete all releases
        Release.query.filter_by(app_id=self.id).delete()
        # delete all permissions
        AppUserRelation.query.filter_by(appname=appname).delete()
        # delete all ELB rules
        rules = ELBRule.get_by_app(appname)
        for rule in rules:
            rule.delete()

        return super(App, self).delete()

    def get_online_entrypoints(self):
        return list(set([c.entrypoint for c in self.container_list]))

    def get_online_pods(self):
        return list(set([c.podname for c in self.container_list]))

    def get_associated_elb_rules(self):
        from citadel.models.loadbalance import ELBRule
        return ELBRule.get_by_app(self.name)

    def to_dict(self):
        d = super(App, self).to_dict()
        d.update({
            'name': self.name,
            'git': self.git,
            'uid': self.uid,
        })
        return d


class Release(BaseModelMixin):
    __tablename__ = 'release'
    __table_args__ = (
        db.UniqueConstraint('app_id', 'sha'),
    )

    sha = db.Column(db.CHAR(64), nullable=False, index=True)
    app_id = db.Column(db.Integer, nullable=False)
    image = db.Column(db.String(255), nullable=False, default='')

    def __str__(self):
        return '<app {r.name} release {r.sha} with image {r.image}>'.format(r=self)

    @classmethod
    def create(cls, app, sha):
        """app must be an App instance"""
        appname = app.name
        commit = get_commit(app.project_name, sha)
        if not commit:
            logger.warn('Error getting commit %s %s', app, sha)
            return None

        specs_text = get_file_content(app.project_name, 'app.yaml', sha)
        if not specs_text:
            logger.warn('Empty specs %s %s', appname, sha)
            return None

        try:
            new_release = cls(sha=commit.id, app_id=app.id)
            db.session.add(new_release)
            db.session.commit()
        except IntegrityError:
            logger.warn('Fail to create Release %s %s, duplicate', appname, sha)
            db.session.rollback()
            return cls.get_by_app_and_sha(appname, sha)

        # after the instance is created, manage app permission through combo
        # permitted_users
        all_permitted_users = set(new_release.get_permitted_users())
        previous_release = new_release.get_previous()
        if previous_release:
            old_folks = set(previous_release.get_permitted_users())
        else:
            old_folks = set()

        come = all_permitted_users - old_folks
        gone = old_folks - all_permitted_users
        logger.info('Release %s change permission: ADD %s, REMOVE %s', sha, come, gone)
        for u in come:
            if not u:
                continue
            AppUserRelation.add(appname, u.id)

        for u in gone:
            if not u:
                continue
            AppUserRelation.delete(appname, u.id)

        # create ELB routes, if there's any
        for combo in new_release.specs.combos.itervalues():
            if not combo.elb:
                continue
            for elbname_and_domain in combo.elb:
                elbname, domain = elbname_and_domain.split()
                r = ELBRule.create(elbname, domain, appname, entrypoint=combo.entrypoint, podname=combo.podname)
                if r:
                    logger.info('Auto create ELBRule %s for app %s', r, appname)
                else:
                    logger.error('Auto create ELBRule failed: app %s', appname)

        return new_release

    def get_previous(self):
        cls = self.__class__
        res = cls.query.filter(cls.id < self.id, cls.app_id == self.app_id)
        return res.first()

    def get_permitted_users(self):
        usernames = self.specs.permitted_users
        permitted_users = [User.get(u) for u in usernames]
        return permitted_users

    @classmethod
    def get(cls, id):
        r = super(Release, cls).get(id)
        # 要检查下 app 还在不在, 不在就失败吧
        if r and r.app:
            return r
        return None

    @classmethod
    def get_by_app(cls, name, start=0, limit=20):
        app = App.get_by_name(name)
        if not app:
            return []

        q = cls.query.filter_by(app_id=app.id).order_by(cls.id.desc())
        return q[start:start + limit]

    @classmethod
    def get_by_app_and_sha(cls, name, sha):
        app = App.get_by_name(name)
        if not app:
            return None

        return cls.query.filter(cls.app_id == app.id, cls.sha.like('{}%'.format(sha))).first()

    @cached_property
    def raw(self):
        """if no build clause in app.yaml, this release is considered raw"""
        return not self.specs.build

    @cached_property
    def short_sha(self):
        return self.sha[:7]

    @cached_property
    def app(self):
        return App.get(self.app_id)

    @cached_property
    def name(self):
        return self.app.name

    @property
    def container_list(self):
        from .container import Container
        return Container.get_by(appname=self.name, sha=self.sha)

    @property
    def gitlab_commit(self):
        commit = get_commit(self.app.project_name, self.sha)
        return commit

    @cached_property
    def specs_text(self):
        specs_text = get_file_content(self.app.project_name, 'app.yaml', self.sha)
        return specs_text

    @cached_property
    def specs(self):
        """load app.yaml from GitLab"""
        specs_text = get_file_content(self.app.project_name, 'app.yaml', self.sha)
        return specs_text and Specs.from_string(specs_text) or None

    @property
    def combos(self):
        return self.specs.combos

    @property
    def entrypoints(self):
        return self.specs.entrypoints

    def update_image(self, image):
        self.image = image
        logger.debug('Set image %s for release %s', image, self.sha)
        db.session.add(self)
        db.session.commit()

    def to_dict(self):
        d = super(Release, self).to_dict()
        d.update({
            'app_id': self.app_id,
            'sha': self.sha,
            'image': self.image,
            'specs': self.specs,
        })
        return d


class AppUserRelation(BaseModelMixin):
    __tablename__ = 'app_user_relation'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'appname'),
    )

    appname = db.Column(db.String(255), nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False)

    @classmethod
    def add(cls, appname, user_id):
        try:
            m = cls(appname=appname, user_id=user_id)
            db.session.add(m)
            db.session.commit()
            return m
        except IntegrityError:
            db.session.rollback()
            return None

    @classmethod
    def delete(cls, appname, user_id):
        cls.query.filter_by(user_id=user_id, appname=appname).delete()
        db.session.commit()

    @classmethod
    def get_user_id_by_appname(cls, appname, start=0, limit=20):
        rs = cls.query.filter_by(appname=appname)
        return [r.user_id for r in rs[start:start + limit] if r]

    @classmethod
    def get_appname_by_user_id(cls, user_id, start=0, limit=20):
        rs = cls.query.filter_by(user_id=user_id)
        if limit:
            res = rs[start:start + limit]
        else:
            res = rs.all()

        return [r.appname for r in res if r]

    @classmethod
    def user_permitted_to_app(cls, user_id, appname):
        user = User.get(user_id)
        if user.privilege:
            return True
        return bool(cls.query.filter_by(user_id=user_id, appname=appname).first())


event.listen(
    App.__table__,
    'after_create',
    DDL('ALTER TABLE %(table)s AUTO_INCREMENT = 10001;'),
)
