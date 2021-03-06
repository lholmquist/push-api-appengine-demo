"""`main` is the top level module for your Bottle application."""

import bottle
from bottle import get, post, route, abort, redirect, template, request, response
from google.appengine.api import app_identity, urlfetch, users
from google.appengine.ext import ndb
import json
import logging
import os
import urllib

DEFAULT_GCM_ENDPOINT = 'https://android.googleapis.com/gcm/send'

TYPE_STOCK = 1
TYPE_CHAT = 2

class GcmSettings(ndb.Model):
    SINGLETON_DATASTORE_KEY = 'SINGLETON'

    @classmethod
    def singleton(cls):
        return cls.get_or_insert(cls.SINGLETON_DATASTORE_KEY)

    endpoint = ndb.StringProperty(
            default=DEFAULT_GCM_ENDPOINT,
            indexed=False)
    sender_id = ndb.StringProperty(default="", indexed=False)
    api_key = ndb.StringProperty(default="", indexed=False)

# TODO: Probably cheaper to have a singleton entity with a repeated property?
class Registration(ndb.Model):
    type = ndb.IntegerProperty(required=True, choices=[TYPE_STOCK, TYPE_CHAT])
    creation_date = ndb.DateTimeProperty(auto_now_add=True)

@route('/setup', method=['GET', 'POST'])
def setup():
    # app.yaml should already have ensured that the user is logged in as admin.
    if not users.is_current_user_admin():
        abort(401, "Sorry, only administrators can access this page.")

    is_dev = os.environ.get('SERVER_SOFTWARE', '').startswith('Development')
    setup_scheme = 'http' if is_dev else 'https'
    setup_url = '%s://%s/setup' % (setup_scheme,
                                   app_identity.get_default_version_hostname())
    if request.url != setup_url:
        redirect(setup_url)

    result = ""
    settings = GcmSettings.singleton()
    if (request.forms.sender_id and request.forms.api_key and
            request.forms.endpoint):
        # Basic CSRF protection (will block some valid requests, like
        # https://1-dot-johnme-gcm.appspot.com/setup but ohwell).
        if request.get_header('Referer') != setup_url:
            abort(403, "Invalid Referer.")
        settings.endpoint = request.forms.endpoint
        settings.sender_id = request.forms.sender_id
        settings.api_key = request.forms.api_key
        settings.put()
        result = 'Updated successfully'
    return template('setup', result=result,
                             endpoint=settings.endpoint,
                             sender_id=settings.sender_id,
                             api_key=settings.api_key)

@get('/manifest.json')
def manifest():
    return {
        'gcm_sender_id': GcmSettings.singleton().sender_id,
        'gcm_user_visible_only': True
    }

@get('/stock')
def stock_redirect():
    redirect("/stock/")

@get('/stock/')
def stock():
    """Single page stock app. Displays stock data and lets users register."""
    return template_with_sender_id('stock')

@get('/stock/admin')
def stock_admin():
    """Lets "admins" trigger stock price drops and clear stock registrations."""
    # Despite the name, this route has no credential checks - don't put anything
    # sensitive here!
    # This template doesn't actually use the sender_id, but we want the warning.
    return template_with_sender_id('stock_admin')

@get('/chat')
def chat_redirect():
    redirect("/chat/")

@get('/chat/')
def chat():
    """Single page chat app."""
    return template_with_sender_id('chat', user_from_get = request.query.get('user') or '')

@get('/admin')
def legacy_chat_admin_redirect():
    redirect("/chat/admin")

@get('/chat/admin')
def chat_admin():
    """Lets "admins" clear chat registrations."""
    # Despite the name, this route has no credential checks - don't put anything
    # sensitive here!
    # This template doesn't actually use the sender_id, but we want the warning.
    return template_with_sender_id('chat_admin')

def template_with_sender_id(*args, **kwargs):
    settings = GcmSettings.singleton()
    if not settings.sender_id or not settings.api_key:
        abort(500, "You need to visit /setup to provide a GCM sender ID and "
                   "corresponding API key")
    kwargs['sender_id'] = settings.sender_id
    return template(*args, **kwargs)

@post('/stock/register')
def register_stock():
    return register(TYPE_STOCK)

@post('/chat/register')
def register_chat():
    return register(TYPE_CHAT)

def register(type):
    """XHR adding a registration ID to our list."""
    if request.forms.registration_id:
        if request.forms.endpoint != DEFAULT_GCM_ENDPOINT:
            abort(500, "Push servers other than GCM are not yet supported.")

        registration = Registration.get_or_insert(request.forms.registration_id,
                                                  type=type)
        registration.put()
    response.status = 201
    return ""

@post('/stock/clear-registrations')
def clear_stock_registrations():
    ndb.delete_multi(Registration.query(Registration.type == TYPE_STOCK)
                                 .fetch(keys_only=True))
    return ""

@post('/chat/clear-registrations')
def clear_chat_registrations():
    ndb.delete_multi(Registration.query(Registration.type == TYPE_CHAT)
                                 .fetch(keys_only=True))
    return ""

@post('/stock/trigger-drop')
def send_stock():
    return send(TYPE_STOCK, '["May", 183]')

@post('/chat/send')
def send_chat():
    return send(TYPE_CHAT, request.forms.message)

def send(type, data):
    """XHR requesting that we send a push message to all users."""
    # TODO: Should limit batches to 1000 registration_ids at a time.
    registration_ids = [r.key.string_id() for r in Registration.query(
                        Registration.type == type).iter()]
    if not registration_ids:
        abort(500, "No registered devices.")
    post_data = json.dumps({
        'registration_ids': registration_ids,
        'data': {
            'data': data,  #request.forms.msg,
        },
        #"collapse_key": "score_update",
        #"time_to_live": 108,
        #"delay_while_idle": true,
    })
    settings = GcmSettings.singleton()
    result = urlfetch.fetch(url=settings.endpoint,
                            payload=post_data,
                            method=urlfetch.POST,
                            headers={
                                'Content-Type': 'application/json',
                                'Authorization': 'key=' + settings.api_key,
                            },
                            validate_certificate=True)
    if result.status_code != 200:
        logging.error("Sending failed:\n" + result.content)
        abort(500, "Sending failed (status code %d)." % result.status_code)
    #return "%d message(s) sent successfully." % len(registration_ids)
    response.status = 202
    return ""

bottle.run(server='gae', debug=True)
app = bottle.app()
