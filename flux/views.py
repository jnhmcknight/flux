# Copyright (c) 2016  Niklas Rosenstein
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import json
import uuid

from . import app, config, utils
from .build import enqueue, terminate_build
from .models import Session, User, Repository, Build, get_target_for, get_public_key
from flask import request, session, redirect, url_for, render_template, abort
from datetime import datetime

API_GOGS = 'gogs'
API_GITHUB = 'github'
API_GITEA = 'gitea'
API_GITBUCKET = 'gitbucket'
API_BITBUCKET = 'bitbucket'
API_BITBUCKET_CLOUD = 'bitbucket-cloud'
API_GITLAB = 'gitlab'

@app.route('/hook/push', methods=['POST'])
@utils.with_io_response(mimetype='text/plain')
@utils.with_logger()
@utils.with_dbsession
def hook_push(logger):
  ''' PUSH event webhook. The URL parameter ``api`` must be specified
  for Flux to expect the correct JSON payload format. Supported values
  for ``api`` are

  * ``gogs``
  * ``github``
  * ``gitea``
  * ``gitbucket``
  * ``bitbucket``
  * ``bitbucket-cloud``
  * ``gitlab``

  If no or an invalid value is specified for this parameter, a 400
  Invalid Request response is generator. '''

  api = request.args.get('api')
  if api not in (API_GOGS, API_GITHUB, API_GITEA, API_GITBUCKET, API_BITBUCKET, API_BITBUCKET_CLOUD, API_GITLAB):
    logger.error('invalid `api` URL parameter: {!r}'.format(api))
    return 400

  logger.info('PUSH event received. Processing JSON payload.')
  try:
    # XXX Determine encoding from Request Headers, if possible.
    data = json.loads(request.data.decode('utf8'))
  except (UnicodeDecodeError, ValueError) as exc:
    logger.error('Invalid JSON data received: {}'.format(exc))
    return 400

  if api == API_GOGS:
    owner = utils.get(data, 'repository.owner.username', str)
    name = utils.get(data, 'repository.name', str)
    ref = utils.get(data, 'ref', str)
    commit = utils.get(data, 'after', str)
    secret = utils.get(data, 'secret', str)
    get_repo_secret = lambda r: r.secret
  elif api == API_GITHUB:
    event = request.headers.get('X-Github-Event')
    if event != 'push':
      logger.error("Payload rejected (expected 'push' event, got {!r})".format(event))
      return 400
    owner = utils.get(data, 'repository.owner.name', str)
    name = utils.get(data, 'repository.name', str)
    ref = utils.get(data, 'ref', str)
    commit = utils.get(data, 'after', str)
    secret = request.headers.get('X-Hub-Signature', '').replace('sha1=', '')
    get_repo_secret = lambda r: utils.get_github_signature(r.secret, request.data)
  elif api == API_GITEA:
    event = request.headers.get('X-Gitea-Event')
    if event != 'push':
      logger.error("Payload rejected (expected 'push' event, got {!r})".format(event))
      return 400
    owner = utils.get(data, 'repository.owner.username', str)
    name = utils.get(data, 'repository.name', str)
    ref = utils.get(data, 'ref', str)
    commit = utils.get(data, 'after', str)
    secret = utils.get(data, 'secret', str)
    get_repo_secret = lambda r: r.secret
  elif api == API_GITBUCKET:
    event = request.headers.get('X-Github-Event')
    if event != 'push':
      logger.error("Payload rejected (expected 'push' event, got {!r})".format(event))
      return 400
    owner = utils.get(data, 'repository.owner.login', str)
    name = utils.get(data, 'repository.name', str)
    ref = utils.get(data, 'ref', str)
    commit = utils.get(data, 'after', str)
    secret = request.headers.get('X-Hub-Signature', '').replace('sha1=', '')
    if secret:
      get_repo_secret = lambda r: utils.get_github_signature(r.secret, request.data)
    else:
      get_repo_secret = lambda r: r.secret
  elif api == API_BITBUCKET:
    event = request.headers.get('X-Event-Key')
    if event != 'repo:refs_changed':
      logger.error("Payload rejected (expected 'repo:refs_changed' event, got {!r})".format(event))
      return 400
    owner = utils.get(data, 'repository.project.name', str)
    name = utils.get(data, 'repository.name', str)
    ref = utils.get(data, 'changes.0.refId', str)
    commit = utils.get(data, 'changes.0.toHash', str)
    secret = request.headers.get('X-Hub-Signature', '').replace('sha256=', '')
    if secret:
      get_repo_secret = lambda r: utils.get_bitbucket_signature(r.secret, request.data)
    else:
      get_repo_secret = lambda r: r.secret
  elif api == API_BITBUCKET_CLOUD:
    event = request.headers.get('X-Event-Key')
    if event != 'repo:push':
      logger.error("Payload rejected (expected 'repo:push' event, got {!r})".format(event))
      return 400
    owner = utils.get(data, 'repository.project.project', str)
    name = utils.get(data, 'repository.name', str)

    ref_type = utils.get(data, 'push.changes.0.new.type', str)
    ref_name = utils.get(data, 'push.changes.0.new.name', str)
    ref = "refs/" + ("heads/" if ref_type == "branch" else "tags/") + ref_name

    commit = utils.get(data, 'push.changes.0.new.target.hash', str)
    secret = None
    get_repo_secret = lambda r: r.secret
  elif api == API_GITLAB:
    event = utils.get(data, 'object_kind', str)
    if event != 'push' and event != 'tag_push':
      logger.error("Payload rejected (expected 'push' or 'tag_push' event, got {!r})".format(event))
      return 400
    owner = utils.get(data, 'project.namespace', str)
    name = utils.get(data, 'project.name', str)
    ref = utils.get(data, 'ref', str)
    commit = utils.get(data, 'checkout_sha', str)
    secret = request.headers.get('X-Gitlab-Token')
    get_repo_secret = lambda r: r.secret
  else:
    assert False, "unreachable"

  if not name:
    logger.error('invalid JSON: no repository name received')
    return 400
  if not owner:
    logger.error('invalid JSON: no repository owner received')
    return 400
  if not ref:
    logger.error('invalid JSON: no Git ref received')
    return 400
  if not commit:
    logger.error('invalid JSON: no commit SHA received')
    return 400
  if len(commit) != 40:
    logger.error('invalid JSON: commit SHA has invalid length')
    return 400
  if secret == None:
    secret = ''

  name = owner + '/' + name

  session = request.db_session
  repo = session.query(Repository).filter_by(name=name).one_or_none()
  if not repo:
    logger.error('PUSH event rejected (unknown repository)')
    return 400
  if get_repo_secret(repo) != secret:
    logger.error('PUSH event rejected (invalid secret)')
    return 400
  if not repo.check_accept_ref(ref):
    logger.info('Git ref {!r} not whitelisted. No build dispatched'.format(ref))
    return 200

  build = Build(repo=repo, commit_sha=commit, num=repo.build_count, ref=ref,
    status=Build.Status_Queued, date_queued=datetime.now(), date_started=None,
    date_finished=None)
  repo.build_count += 1
  session.add(repo)
  session.add(build)
  session.commit()

  enqueue(build)
  logger.info('Build #{} for repository {} queued'.format(build.num, repo.name))
  logger.info(utils.strip_url_path(config.app_url) + build.url())
  return 200


@app.route('/')
@utils.requires_auth
@utils.with_dbsession
def dashboard():
  session = request.db_session
  context = {}
  context['builds'] = session.query(Build).order_by(Build.date_queued.desc()).limit(10).all()
  context['user'] = request.user
  return render_template('dashboard.html', **context)

@app.route('/repositories')
@utils.requires_auth
@utils.with_dbsession
def repositories():
  session = request.db_session
  repositories = session.query(Repository).order_by(Repository.name).all()
  return render_template('repositories.html', user=request.user, repositories=repositories)

@app.route('/users')
@utils.requires_auth
@utils.with_dbsession
def users():
  if not request.user.can_manage:
    return abort(403)
  session = request.db_session
  users = session.query(User).all()
  return render_template('users.html', user=request.user, users=users)

@app.route('/integration')
@utils.requires_auth
def integration():
  if not request.user.can_manage:
    return abort(403)
  return render_template('integration.html', user=request.user, public_key=get_public_key())

@app.route('/login', methods=['GET', 'POST'])
@utils.with_dbsession
def login():
  db_session = request.db_session
  errors = []
  if request.method == 'POST':
    user_name = request.form['user_name']
    user_password = request.form['user_password']
    user = User.get_by(db_session, user_name, utils.hash_pw(user_password))
    if user:
      # XXX User login keys rather than storing the password hash
      # in a cookie. See issue #16.
      session['user_name'] = user.name
      session['user_passhash'] = user.passhash
      return redirect(url_for('dashboard'))
    errors.append('Username or password invalid.')
  return render_template('login.html', errors=errors)


@app.route('/logout')
@utils.with_dbsession
def logout():
  session.pop('user_name', '')
  session.pop('user_passhash', '')
  return redirect(url_for('dashboard'))


@app.route('/repo/<path:path>')
@utils.requires_auth
@utils.with_dbsession
def view_repo(path):
  session = request.db_session
  repo = get_target_for(session, path)

  if not isinstance(repo, Repository):
    return abort(404)

  context = {}
  page_size = 10

  try:
    context['page_number'] = int(request.args.get('page', 1))
  except:
    context['page_number'] = 1
  context['page_from'] = (context['page_number'] - 1) * page_size
  context['page_to'] = context['page_from'] + page_size
  context['next_page'] = None if context['page_number'] <= 1 else context['page_number'] - 1
  context['previous_page'] = None if len(repo.builds) <= context['page_to'] else context['page_number'] + 1

  return render_template('view_repo.html', user=request.user, repo=repo, **context)


@app.route('/build/<path:path>')
@utils.requires_auth
@utils.with_dbsession
def view_build(path):
  session = request.db_session
  build = get_target_for(session, path)
  if not isinstance(build, Build):
    return abort(404)

  restart = request.args.get('restart', '').strip().lower() == 'true'
  if restart:
    if build.status != Build.Status_Building:
      build.delete()
      build.status = Build.Status_Queued
      build.date_started = None
      build.date_finished = None
      request.db_session.add(build)
      request.db_session.commit()
      enqueue(build)
    return redirect(build.url())

  stop = request.args.get('stop', '').strip().lower() == 'true'
  if stop:
    if build.status == Build.Status_Queued:
      build.status = Build.Status_Stopped
    elif build.status == Build.Status_Building:
      terminate_build(build)
    return redirect(build.url())

  return render_template('view_build.html', user=request.user, build=build)


@app.route('/edit/repo', methods=['GET', 'POST'], defaults={'repo_id': None})
@app.route('/edit/repo/<int:repo_id>', methods=['GET', 'POST'])
@utils.requires_auth
@utils.with_dbsession
def edit_repo(repo_id):
  session = request.db_session
  if not request.user.can_manage:
    return abort(403)
  if repo_id is not None:
    repo = session.query(Repository).get(repo_id)
  else:
    repo = None

  errors = []
  if request.method == 'POST':
    secret = request.form.get('repo_secret', '')
    clone_url = request.form.get('repo_clone_url', '')
    repo_name = request.form.get('repo_name', '').strip()
    ref_whitelist = request.form.get('repo_ref_whitelist', '')
    if len(repo_name) < 3 or repo_name.count('/') != 1:
      errors.append('Invalid repository name. Format must be owner/repo')
    if not clone_url:
      errors.append('No clone URL specified')
    other = session.query(Repository).filter_by(name=repo_name).one_or_none()
    if (other and not repo) or (other and other.id != repo.id):
      errors.append('Repository {!r} already exists'.format(repo_name))
    if not errors:
      if not repo:
        repo = Repository(name=repo_name, clone_url=clone_url, secret=secret,
          build_count=0, ref_whitelist=ref_whitelist)
      else:
        repo.name = repo_name
        repo.clone_url = clone_url
        repo.secret = secret
        repo.ref_whitelist = ref_whitelist
      session.add(repo)
      session.commit()
      return redirect(repo.url())

  return render_template('edit_repo.html', user=request.user, repo=repo, errors=errors)


@app.route('/user/new', defaults={'user_id': None}, methods=['GET', 'POST'])
@app.route('/user/<int:user_id>', methods=['GET', 'POST'])
@utils.requires_auth
@utils.with_dbsession
def edit_user(user_id):
  session = request.db_session
  cuser = None
  if user_id is not None:
    cuser = session.query(User).get(user_id)
    if not cuser:
      return abort(404)
    if cuser.id != request.user.id and not request.user.can_manage:
      return abort(403)
  elif not request.user.can_manage:
    return abort(403)

  errors = []
  if request.method == 'POST':
    if not cuser and not request.user.can_manage:
      return abort(403)

    user_name = request.form.get('user_name')
    password = request.form.get('user_password')
    can_manage = request.form.get('user_can_manage') == 'on'
    can_view_buildlogs = request.form.get('user_can_view_buildlogs') == 'on'
    can_download_artifacts = request.form.get('user_can_download_artifacts') == 'on'

    if not cuser:  # Create a new user
      assert request.user.can_manage
      other = session.query(User).filter_by(name=user_name).one_or_none()
      if other:
        errors.append('User {!r} already exists'.format(user_name))
      elif len(user_name) == 0:
        errors.append('Username is empty')
      elif len(password) == 0:
        errors.append('Password is empty')
      else:
        cuser = User(name=user_name, passhash=utils.hash_pw(password),
          can_manage=can_manage, can_view_buildlogs=can_view_buildlogs,
          can_download_artifacts=can_download_artifacts)
    else:  # Update user settings
      if password:
        cuser.passhash = utils.hash_pw(password)
      # The user can only update privileges if he has managing privileges.
      if request.user.can_manage:
        cuser.can_manage = can_manage
        cuser.can_view_buildlogs = can_view_buildlogs
        cuser.can_download_artifacts = can_download_artifacts
    if not errors:
      session.add(cuser)
      session.commit()
      return redirect(cuser.url())

  return render_template('edit_user.html', user=request.user, cuser=cuser,
    errors=errors)


@app.route('/download/<int:build_id>/<string:data>')
@utils.requires_auth
@utils.with_dbsession
def download(build_id, data):
  if data not in (Build.Data_Artifact, Build.Data_Log):
    return abort(404)
  build = request.db_session.query(Build).get(build_id)
  if not build:
    return abort(404)
  if not build.check_download_permission(data, request.user):
    return abort(403)
  if not build.exists(data):
    return abort(404)
  mime = 'application/zip' if data == Build.Data_Artifact else 'text/plain'
  download_name = "{}-{}.{}".format(build.repo.name.replace("/", "_"), build.num, "zip" if data == Build.Data_Artifact else 'log')
  return utils.stream_file(build.path(data), name=download_name, mime=mime)


@app.route('/delete')
@utils.requires_auth
@utils.with_dbsession
def delete():
  repo_id = request.args.get('repo_id', '')
  build_id = request.args.get('build_id', '')
  user_id = request.args.get('user_id', '')

  session = request.db_session
  delete_target = None
  return_to = 'dashboard'
  if build_id:
    delete_target = session.query(Build).get(build_id)
    return_to = delete_target.repo.url()
    if not request.user.can_manage:
      return abort(403)
  elif repo_id:
    delete_target = session.query(Repository).get(repo_id)
    return_to = url_for('repositories')
    if not request.user.can_manage:
      return abort(403)
  elif user_id:
    delete_target = session.query(User).get(user_id)
    return_to = url_for('users')
    if delete_target and delete_target.id != request.user.id and not request.user.can_manage:
      return abort(403)

  if not delete_target:
    return abort(404)

  try:
    session.delete(delete_target)
    session.commit()
  except Build.CanNotDelete as exc:
    session.rollback()
    utils.flash(str(exc))
    referer = request.headers.get('Referer', return_to)
    return redirect(referer)

  utils.flash('{} deleted'.format(type(delete_target).__name__))
  return redirect(return_to)


@app.route('/build')
@utils.requires_auth
@utils.with_dbsession
def build():
  repo_id = request.args.get('repo_id', '')
  ref_name = request.args.get('ref', '')
  session = request.db_session
  if not repo_id or not ref_name:
    return abort(400)
  if not request.user.can_manage:
    return abort(403)

  # TODO: Determine the commit SHA.
  commit = '0' * 32
  repo = session.query(Repository).get(repo_id)
  build = Build(repo=repo, commit_sha=commit, num=repo.build_count, ref=ref_name,
    status=Build.Status_Queued, date_queued=datetime.now(), date_started=None,
    date_finished=None)
  repo.build_count += 1
  session.add(repo)
  session.add(build)
  session.commit()

  enqueue(build)
  print('Build #{} for repository {} queued'.format(build.num, repo.name))
  print(utils.strip_url_path(config.app_url) + build.url())

  return redirect(repo.url())

@app.route('/ping-repo', methods=['POST'])
@utils.requires_auth
def ping_repo():
  repo_url = request.form.get('url')
  res = utils.ping_repo(repo_url)
  if (res == 0):
    return 'ok', 200
  else:
    return 'fail', 404

@app.errorhandler(403)
def error_403(e):
  return render_template('403.html'), 403


@app.errorhandler(404)
def error_404(e):
  return render_template('404.html'), 404


@app.errorhandler(500)
def error_500(e):
  return render_template('500.html'), 500
