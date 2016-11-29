# coding: utf-8
import json

import yaml
from flask import g, jsonify, Response, request

from citadel.libs.agent import EruAgentError, EruAgentClient
from citadel.libs.datastructure import AbortDict
from citadel.libs.view import create_api_blueprint
from citadel.models.app import Release
from citadel.models.env import Environment
from citadel.models.gitlab import get_project_name, get_file_content
from citadel.rpc import core
from citadel.tasks import create_container, remove_container, ActionError, upgrade_container, action_stream, celery_task_stream_response, build_image


# 把action都挂在/api/:version/下, 不再加前缀
# 也不需要他帮忙自动转JSON了
bp = create_api_blueprint('action', __name__, jsonize=False)


@bp.route('/build', methods=['POST'])
def build():
    """
    可以这么玩玩:
    $ http --stream POST localhost:5000/api/v1/build repo=git@gitlab.ricebook.net:tonic/ci-test.git sha=1d74377e99dcfb3fd892f9eaeab91e1e229179ba uid=4401
    """
    data = AbortDict(request.get_json())

    # TODO 参数需要类型校验
    repo = data['repo']
    sha = data['sha']
    artifact = data.get('artifact', '')
    uid = data.get('uid', '')
    gitlab_build_id = data.get('gitlab_build_id', '')

    async_result = build_image.delay(repo, sha, uid, artifact, gitlab_build_id)
    return Response(celery_task_stream_response(async_result.task_id), mimetype='application/json')


@bp.route('/deploy', methods=['POST'])
def deploy():
    data = AbortDict(request.get_json())

    # TODO 参数需要类型校验
    repo = data['repo']
    sha = data['sha']
    project_name = get_project_name(repo)
    specs_text = get_file_content(project_name, 'app.yaml', sha)
    if not specs_text:
        raise ActionError(400, 'repo %s, %s does not have app.yaml in root directory' % (repo, sha))
    specs = yaml.load(specs_text)
    appname = specs.get('appname', '')
    release = Release.get_by_app_and_sha(appname, sha)
    if not release:
        raise ActionError(400, 'repo %s, %s does not have the right appname in app.yaml' % (repo, sha))

    networks = {key: '' for key in data.get('networks', {})}
    envname = data.get('env', '')
    env = Environment.get_by_app_and_env(appname, envname)
    full_envs = env and env.to_env_vars() or []
    extra_env = data.get('extra_env', [])
    full_envs.extend(extra_env)

    raw = bool(data.get('raw', ''))
    deploy_options = {
        'specs': release.specs_text,
        'appname': appname,
        'image': specs.base if raw else release.image,
        'podname': data['podname'],
        'nodename': data.get('nodename', ''),
        'entrypoint': data['entrypoint'],
        'cpu_quota': float(data['cpu_quota']),
        'count': int(data['count']),
        'memory': int(data.get('memory', 0)),
        'networks': networks,
        'env': full_envs,
        'raw': raw,
        'debug': bool(data.get('debug', False)),
        'extra_args': data.get('extra_args', ''),
    }

    async_result = create_container.delay(deploy_options, sha=data['sha'], user_id=g.user.id, envname=envname)
    return Response(celery_task_stream_response(async_result.task_id), mimetype='application/json')


@bp.route('/remove', methods=['POST'])
def remove():
    data = AbortDict(request.get_json())
    ids = data['ids']
    async_result = remove_container.delay(ids, user_id=g.user.id)
    return Response(celery_task_stream_response(async_result.task_id), mimetype='application/json')


@bp.route('/upgrade', methods=['POST'])
def upgrade():
    data = AbortDict(request.get_json())
    ids = data['ids']
    repo = data['repo']
    sha = data['sha']

    q = upgrade_container(ids, repo, sha)
    return Response(action_stream(q), mimetype='application/json')


@bp.route('/log', methods=['POST'])
def get_log():
    data = AbortDict(request.get_json())
    appname = data['appname']
    podname = data['podname']
    nodename = data['nodename']

    node = core.get_node(podname, nodename)
    if not node:
        raise ActionError(400, 'Node %s not found' % nodename)

    # use ActionError instead of EruAgentError to client
    client = EruAgentClient(node.ip)
    try:
        resp = client.log(appname)
    except EruAgentError as e:
        raise ActionError(400, e.message)

    def log_producer():
        for data in resp:
            yield json.dumps(data) + '\n'

    return Response(log_producer(), mimetype='application/json')


@bp.errorhandler(ActionError)
def error_handler(e):
    return jsonify({'error': e.message}), e.code
