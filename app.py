from hashlib import sha256
import hmac
import os
import threading
import urllib.parse

from dropbox.client import DropboxClient, DropboxOAuth2Flow
from flask import abort, Flask, redirect, render_template, request, session,\
                  url_for
from markdown import markdown
import redis

app = Flask(__name__, instance_relative_config=True)
app.debug = False
app.config.from_pyfile('dev_config.py', silent=True)

redis_url = os.environ['REDISTOGO_URL']
redis_client = redis.from_url(redis_url, decode_responses=True)

# App key and secret from the App console (dropbox.com/developers/apps)
APP_KEY = os.environ['APP_KEY']
APP_SECRET = os.environ['APP_SECRET']

# A random secret used by Flask to encrypt session data cookies
app.secret_key = os.environ['FLASK_SECRET_KEY']


def get_url(route):
    '''Generate a proper URL, forcing HTTPS if not running locally'''
    host = urllib.parse.urlparse(request.url).hostname
    url = url_for(route, _external=True,
                  _scheme=('http' if host in ('127.0.0.1', 'localhost')
                           else 'https'))
    return url


def get_flow():
    return DropboxOAuth2Flow(
        APP_KEY,
        APP_SECRET,
        get_url('oauth_callback'),
        session,
        'dropbox-csrf-token')


@app.route('/welcome')
def welcome():
    return render_template('welcome.html',
                           redirect_url=get_url('oauth_callback'),
                           webhook_url=get_url('webhook'),
                           home_url=get_url('index'), app_key=APP_KEY)


@app.route('/oauth_callback')
def oauth_callback():
    '''Callback function for when the user returns from OAuth.'''

    access_token, uid, extras = get_flow().finish(request.args)

    # Extract and store the access token for this user
    redis_client.hset('tokens', uid, access_token)

    process_user(uid)

    return redirect(url_for('done'))


def process_user(uid):
    '''Call /delta for the given user ID and process any changes.'''

    # OAuth token for the user
    token = redis_client.hget('tokens', uid)

    # /delta cursor for the user (None the first time)
    cursor = redis_client.hget('cursors', uid)

    client = DropboxClient(token)
    has_more = True

    while has_more:
        result = client.delta(cursor)

        for path, metadata in result['entries']:

            # Ignore deleted files, folders, and non-markdown files
            if (metadata is None or
                    metadata['is_dir'] or
                    not path.endswith('.md')):
                continue

            # Convert to Markdown and store as <basename>.html
            response, metadata = client.get_file_and_metadata(path)
            md = response.read().decode()
            html = markdown(md, extensions=['gfm'])
            html_name = path[:-3] + '.html'
            client.put_file(html_name, html, overwrite=True)

            # Include URL to published file in HTML comment at top of Markdown
            # file
            if '<!-- Published file url:' != md.split('\n')[0]:
                share_url = client.share(html_name, short_url=False).get('url')
                file_key = share_url.split('/')[4]

                url_name = urllib.parse.quote(html_name)
                url_comment = ('<!-- Published file url:\n'
                               'https://dl.dropboxusercontent.com/s/'
                               '{}{}\n-->\n'.format(file_key, url_name))
                md = url_comment + md
                client.put_file(path, md, overwrite=True)

        # Update cursor
        cursor = result['cursor']
        redis_client.hset('cursors', uid, cursor)

        # Repeat only if there's more to do
        has_more = result['has_more']


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login')
def login():
    return redirect(get_flow().start())


@app.route('/done')
def done():
    return render_template('done.html')


def validate_request():
    '''Validate that the request is properly signed by Dropbox.
       (If not, this is a spoofed webhook.)'''

    signature = request.headers.get('X-Dropbox-Signature')
    return signature == hmac.new(APP_SECRET.encode(), request.data,
                                 sha256).hexdigest()


@app.route('/webhook', methods=['GET'])
def challenge():
    '''Respond to the webhook challenge (GET request) by echoing back the
    challenge parameter.'''

    return request.args.get('challenge')


@app.route('/webhook', methods=['POST'])
def webhook():
    '''Receive a list of changed user IDs from Dropbox and process each.'''

    # Make sure this is a valid request from Dropbox
    if not validate_request():
        abort(403)

    for uid in request.json['delta']['users']:
        # We need to respond quickly to the webhook request, so we do the
        # actual work in a separate thread. For more robustness, it's a
        # good idea to add the work to a reliable queue and process the queue
        # in a worker process.
        threading.Thread(target=process_user, args=(uid,)).start()
    return ''


if __name__ == '__main__':
    app.run()
