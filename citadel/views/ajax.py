# -*- coding: utf-8 -*-
import json

from flask import Blueprint, jsonify, Response, g, request, abort

from citadel.config import CONTAINER_DEBUG_LOG_CHANNEL, ELB_APP_NAME, ELB_POD_NAME
from citadel.ext import rds
from citadel.libs.json import jsonize
from citadel.libs.utils import logger, to_number
from citadel.libs.view import DEFAULT_RETURN_VALUE, ERROR_CODES
from citadel.models.app import AppUserRelation, Release, App
from citadel.models.container import Container
from citadel.models.env import Environment
from citadel.models.oplog import OPType, OPLog
from citadel.rpc import core
from citadel.tasks import ActionError, create_elb_instance_upon_containers, create_container, remove_container, upgrade_container, celery_task_stream_response
from citadel.views.helper import bp_get_app, bp_get_balancer


bp = Blueprint('ajax', __name__, url_prefix='/ajax')


def _error_hanlder(error):
    return jsonify({'error': error.description}), error.code


for code in ERROR_CODES:
    bp.errorhandler(code)(_error_hanlder)


@bp.route('/app/<name>/delete-env', methods=['POST'])
@jsonize
def delete_app_env(name):
    envname = request.form['env']
    app = bp_get_app(name)

    # 记录oplog
    OPLog.create(g.user.id, OPType.DELETE_ENV, app.name, content={'envname': envname})

    env = Environment.get_by_app_and_env(app.name, envname)
    if env:
        logger.info('Env [%s] for app [%s] deleted', envname, name)
        env.delete()
    return DEFAULT_RETURN_VALUE


@bp.route('/app/<name>/online-entrypoints', methods=['GET'])
@jsonize
def get_app_online_entrypoints(name):
    app = bp_get_app(name)
    return app.get_online_entrypoints()


@bp.route('/app/<name>/online-pods', methods=['GET'])
@jsonize
def get_app_online_pods(name):
    app = bp_get_app(name)
    return app.get_online_pods()


@bp.route('/app/<name>/backends')
@jsonize
def get_app_backends(name):
    return {}


@bp.route('/release/<release_id>/deploy', methods=['POST'])
def deploy_release(release_id):
    """部署的ajax接口, oplog在对应action里记录."""
    release = Release.get(release_id)
    if not release:
        abort(404, 'Release %s not found' % release_id)

    if release.name == ELB_APP_NAME:
        abort(400, 'Do not deploy %s through this API' % ELB_APP_NAME)

    specs = release.specs
    if not (specs and specs.entrypoints):
        abort(404, 'Release %s has no entrypoints')

    # TODO: args validation
    payload = request.get_json()
    appname = release.name

    envname = payload.get('envname', '')
    env = Environment.get_by_app_and_env(appname, envname)
    env_vars = env and env.to_env_vars() or []
    extra_env = [s.strip() for s in payload.get('extra_env', '').split(';')]
    extra_env = [s for s in extra_env if s]
    env_vars.extend(extra_env)

    # 这里来的就都走自动分配吧
    networks = {key: '' for key in payload['networks']}
    debug = payload.get('debug', False)

    deploy_options = {
        'specs': release.specs_text,
        'appname': appname,
        'image': release.image,
        'podname': payload['podname'],
        'nodename': payload.get('nodename', ''),
        'entrypoint': payload['entrypoint'],
        'cpu_quota': float(payload.get('cpu', 1)),
        'count': int(payload.get('count', 1)),
        'memory': to_number(payload.get('memory', '512MB')),
        'networks': networks,
        'env': env_vars,
        'raw': release.raw,
        'debug': debug,
    }

    async_result = create_container.delay(deploy_options=deploy_options,
                                          sha=release.sha,
                                          envname=envname,
                                          user_id=g.user.id)

    def generate_stream_response():
        """relay grpc message, if in debug mode, stream logs as well"""
        for msg in celery_task_stream_response(async_result.task_id):
            yield msg

        async_result.wait(timeout=40)
        if async_result.failed():
            logger.debug('Task %s failed, dumping traceback', async_result.task_id)
            yield json.dumps({'success': False, 'error': async_result.traceback})

        if debug:
            debug_log_channel = CONTAINER_DEBUG_LOG_CHANNEL.format(release.name)
            debug_log_pubsub = rds.pubsub()
            debug_log_pubsub.psubscribe(debug_log_channel)
            for item in debug_log_pubsub.listen():
                logger.debug('Stream response emit debug log: %s', item)
                yield json.dumps(item)

    return Response(generate_stream_response(), mimetype='text/event-stream')


@bp.route('/release/<release_id>/entrypoints')
@jsonize
def get_release_entrypoints(release_id):
    release = Release.get(release_id)
    if not release:
        abort(404, 'Release %s not found' % release_id)

    if not (release.specs and release.specs.entrypoints):
        abort(404, 'Release %s has no entrypoints')

    return release.specs.entrypoints.keys()


@bp.route('/rmcontainer', methods=['POST'])
@jsonize
def remove_containers():
    # 过滤掉ELB的容器, ELB不要走这个方式下线
    payload = request.get_json()
    container_ids = payload['container_id']
    if isinstance(container_ids, basestring):
        container_ids = [container_ids]

    containers = [Container.get_by_container_id(i) for i in container_ids]
    # mark removing so that users would see some changes, but the actual
    # removing happends in celery tasks
    should_remove = []
    for c in containers:
        if not c:
            continue
        if c.appname == ELB_APP_NAME:
            return {'error': 'Cannot delete ELB container here'}, 400
        c.mark_removing()
        should_remove.append(c.container_id)

    remove_container.delay(should_remove, user_id=g.user.id)
    return DEFAULT_RETURN_VALUE


@bp.route('/upgrade-container', methods=['POST'])
def upgrade_containers():
    # TODO: validation
    payload = request.get_json()
    container_ids = payload['container_ids']
    sha = payload['sha']
    appname = payload['appname']

    app = App.get_by_name(appname)
    if not app:
        abort(400, 'App %s not found' % appname)
    if app.name == ELB_APP_NAME:
        abort(400, 'Do not upgrade %s through this API' % ELB_APP_NAME)

    async_result = upgrade_container.delay(container_ids, app.git, sha)
    return Response(celery_task_stream_response(async_result.task_id), mimetype='application/json')


@bp.route('/pods')
@jsonize
def get_all_pods():
    return core.list_pods()


@bp.route('/pod/<name>/nodes')
@jsonize
def get_pod_nodes(name):
    return core.get_pod_nodes(name)


@bp.route('/loadbalance', methods=['POST'])
@jsonize
def create_loadbalance():
    # TODO: validation
    logger.debug('Got create_loadbalance payload: %s', request.data)
    payload = request.get_json()
    release = Release.get(payload['releaseid'])
    envname = payload['envname']
    env = Environment.get_by_app_and_env(ELB_APP_NAME, envname)
    env_vars = env and env.to_env_vars() or []
    name = env.get('ELBNAME', 'unnamed')
    user_id = g.user.id
    sha = release.sha

    deploy_options = {
        'specs': release.specs_text,
        'appname': ELB_APP_NAME,
        'image': release.image,
        'podname': ELB_POD_NAME,
        'nodename': payload.get('nodename', ''),
        'entrypoint': payload['entrypoint'],
        'cpu_quota': float(payload.get('cpu', 2)),
        'count': 1,
        'memory': to_number('2GB'),
        'networks': {},
        'env': env_vars,
    }
    try:
        # TODO: slow, async?
        container_ids = create_container(deploy_options=deploy_options,
                                         sha=sha, envname=envname,
                                         user_id=user_id)
        create_elb_instance_upon_containers(container_ids, name, sha,
                                            comment=payload['comment'],
                                            user_id=user_id)
    except ActionError as e:
        return {'error': e.message}, 500
    return DEFAULT_RETURN_VALUE


@bp.route('/loadbalance/<id>/remove', methods=['POST'])
@jsonize
def remove_loadbalance(id):
    elb = bp_get_balancer(id)
    if elb.is_only_instance():
        elb.clear_rules()

    try:
        remove_container(elb.container_id, user_id=g.user.id)
        elb.delete()
    except ActionError as e:
        return {'error': e.message}, 500
    return DEFAULT_RETURN_VALUE


@bp.route('/admin/revoke-app', methods=['POST'])
@jsonize
def revoke_app():
    user_id = request.form['user_id']
    name = request.form['name']
    AppUserRelation.delete(name, user_id)
    return DEFAULT_RETURN_VALUE


@bp.before_request
def access_control():
    # loadbalance和admin的不是admin就不要乱搞了
    if not g.user.privilege and (request.path.startswith('/ajax/admin') or request.path.startswith('/ajax/loadbalance')):
        abort(403, 'Only for admin')
