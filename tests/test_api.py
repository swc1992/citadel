# -*- coding: utf-8 -*-
import json
import pytest
from flask import url_for
from humanfriendly import parse_size

from .prepare import default_appname, make_specs_text, default_ports, make_specs, core_online, default_podname
from .conftest import json_headers
from citadel.models.app import Release


def test_register_app(test_db, client):
    appname = 'python-helloworld'
    sha = '3ff0138208ce41693d8ab1b96326b660cad34bef'
    git = 'git@github.com:dbarnett/python-helloworld.git'
    entrypoints = {
        'web': {
            'cmd': 'python -m http.server',
            'ports': default_ports,
        },
        'hello': {
            'cmd': 'python helloworld.py',
        },
    }
    specs_text = make_specs_text(appname=appname, entrypoints=entrypoints)
    specs = make_specs(appname=appname, entrypoints=entrypoints)
    app_data = {
        'appname': appname,
        'git': git,
        'sha': sha,
        'specs_text': specs_text,
        'branch': 'master',
        'commit_message': '我一定行',
        'author': 'timfeirg',
    }
    res = client.post(url_for('app.register_release'),
                      data=json.dumps(app_data),
                      headers=json_headers)
    assert res.status_code == 200
    release = Release.get_by_app_and_sha(appname, sha[:7])
    assert release.app.git == git
    assert release.commit_message == '我一定行'
    assert release.specs._raw == specs._raw

    # test duplicate, and test POST using HTTP form BTW
    res = client.post(url_for('app.register_release'),
                      data=app_data)
    assert res.status_code == 400
    assert 'IntegrityError' in res.json['error']


def test_combo(test_db, client):
    combo_name = 'another'
    data = {
        'name': combo_name,
        'entrypoint_name': 'web',
        'podname': 'release',
        'networks': ['release'],
        'cpu_quota': 4.5,
        'memory': '512MB',
        'count': 4,
        'envname': 'prod',
    }
    res = client.post(url_for('app.create_combo', appname=default_appname),
                      data=json.dumps(data),
                      headers=json_headers)
    assert res.status_code == 200
    combo = res.json
    assert combo['cpu_quota'] == 4.5
    assert combo['memory'] == parse_size('512MB', binary=True)
    assert combo['networks'] == ['release']

    res = client.delete(url_for('app.delete_combo', appname=default_appname),
                        data=json.dumps({'name': combo_name}),
                        headers=json_headers)
    assert res.status_code == 200

    # test typo
    data['network'] = data.pop('networks')
    res = client.post(url_for('app.create_combo', appname=default_appname),
                      data=json.dumps(data),
                      headers=json_headers)
    assert res.status_code == 422


@pytest.mark.skipif(not core_online, reason='needs eru-core')
def test_pod_meta(test_db, client):
    # nothing to assert here, just to see if everything works
    all_pods = client.get(url_for('pod.get_all_pods')).json
    assert len(all_pods) == 1
    podname = all_pods[0]['name']
    assert podname == default_podname

    pod_info = client.get(url_for('pod.get_pod', name=podname)).json
    assert pod_info
    pod_nodes = client.get(url_for('pod.get_pod_nodes', name=podname)).json
    assert pod_nodes
    networks = client.get(url_for('pod.list_networks', name=podname)).json
    assert networks


def test_app_env(test_db, client):
    res = client.get(url_for('app.get_app_envs', appname=default_appname))
    assert res.json == {}

    test_env_name = 'testenv'
    test_env = {
        'foo': '\'',
        'FOO': '\"'
    }
    res = client.post(url_for('app.create_app_env', appname=default_appname, envname=test_env_name),
                      data=json.dumps(test_env),
                      headers=json_headers)
    assert res.status_code == 200

    res = client.get(url_for('app.get_app_env', appname=default_appname, envname=test_env_name))
    assert res.json == test_env

    bad_env = {'ERU_MEMORY': 23}
    res = client.post(url_for('app.create_app_env', appname=default_appname, envname='badenv'),
                      data=json.dumps(bad_env),
                      headers=json_headers)
    assert res.status_code == 400
    assert 'Cannot add these keys' in res.json['error']
    assert 'ERU_MEMORY' in res.json['error']

    client.post(url_for('app.create_app_env', appname=default_appname, envname='anotherenv'),
                data=json.dumps({'foo': 'whatever'}),
                headers=json_headers)
    res = client.delete(url_for('app.delete_app_env', appname=default_appname, envname='anotherenv'))
    assert res.status_code == 200

    res = client.get(url_for('app.get_app_envs', appname=default_appname))
    assert res.json == {test_env_name: test_env}
