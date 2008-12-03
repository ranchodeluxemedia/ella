import email
import logging
from time import strftime
import smtplib
from datetime import datetime
from django import http
from django import forms
from django.core.urlresolvers import reverse
from django.template import RequestContext, loader, Context
from django.shortcuts import render_to_response
from django.template.defaultfilters import slugify
from django.utils.translation import ugettext, ugettext_lazy as _
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth import authenticate, login, logout, get_user
from django.contrib.auth.models import User, AnonymousUser
from django.views.generic.list_detail import object_list
from django.contrib.formtools.preview import FormPreview
from django.core.paginator import QuerySetPaginator
from django.conf import settings

from ella.discussions.models import BannedString, BannedUser, Topic, TopicThread, \
PostViewed, DuplicationError, get_comments_on_thread
from ella.discussions.cache import comments_on_thread__by_submit_date, get_key_comments_on_thread__by_submit_date, \
comments_on_thread__spec_filter, get_key_comments_on_thread__spec_filter
from ella.comments.models import Comment
from ella.core.cache.utils import get_cached_object_or_404, get_cached_list, cache_this, \
normalize_key, delete_cached_object


STATE_UNAUTHORIZED = 'unauthorized'
STATE_EMPTY = 'empty'
STATE_OK = 'ok'
STATE_INVALID = 'invalid'
STATE_NOT_ACTIVE = 'not_active'
STATE_BAD_LOGIN_OR_PASSWORD = 'bad_password'

DISCUSSIONS_PAGINATE_BY = getattr(settings, 'DISCUSSIONS_PAGINATE_BY', 5)


class QuestionForm(forms.Form):
    """
    title = Question._meta.get_field('title').formfield()
    nickname = Question._meta.get_field('nickname').formfield()
    email = Question._meta.get_field('email').formfield()
    description = Question._meta.get_field('description').formfield()
    """
    content = forms.CharField(required=True, widget=forms.Textarea)
    nickname = forms.CharField(required=True)
    email = forms.EmailField(required=True)

class ThreadForm(QuestionForm):
    title = forms.CharField(required=True)
    content = forms.CharField(required=True, widget=forms.Textarea)

class LoginForm(forms.Form):
    username = forms.CharField(required=True)
    password = forms.CharField(required=True, widget=forms.PasswordInput)

def get_ip(request):
    if 'HTTP_X_FORWARDED_FOR' in request.META:
        return request.META['HTTP_X_FORWARDED_FOR']
    return request.META['REMOTE_ADDR']

def add_post(content, thread, user = False, nickname = False, email = False, ip='0.0.0.0'):
    """
    PARAMS
    content: post content
    thread: TopicThread instance
    ip: IP address
    """
    # invalidate cached thread posts
    delete_cached_object(get_key_comments_on_thread__by_submit_date(None, thread))
    delete_cached_object(get_key_comments_on_thread__spec_filter(None, thread))

    content = filter_banned_strings(content)
    comment_set = get_comments_on_thread(thread).order_by('submit_date')
    CT_THREAD = ContentType.objects.get_for_model(TopicThread)
    parent = None
    if comment_set.count() > 0:
        parent = comment_set[0]

    if (user):
        cmt = Comment(
            content=content,
            subject='',
            ip_address=ip,
            target_ct=CT_THREAD,
            target_id=thread.id,
            parent=parent,
            user=user,
)

        cmt.save()
        # post is viewed by its autor
        CT = ContentType.objects.get_for_model(Comment)
        post_viewed = PostViewed(target_ct=CT, target_id=cmt._get_pk_val(), user=user)
        post_viewed.save()

    elif nickname and email:
        cmt = Comment(
            content=content,
            subject='',
            ip_address=ip,
            target_ct=CT_THREAD,
            target_id=thread.id,
            parent=parent,
            nickname=nickname,
            email=email,
)

        cmt.save()

    else:
        raise Exception("Either user or nickname and email params required!")

def paginate_queryset_for_request(request, qset):
    """ returns appropriate page for view. Page number should
        be set in GET variable 'p', if not set first page is returned.
    """
    paginate_by = DISCUSSIONS_PAGINATE_BY
    # ugly son of a bitch - adding object property at runtime?!
    for i, c in enumerate(qset):
        ct = ContentType.objects.get_for_model(c)
        setattr(c, 'item_number', i + 1)
        #setattr(
           # c,
          #  'get_admin_url',
         #   reverse('discussions_admin', args=['%s/%s/%d' % (ct.app_label, ct.model, c._get_pk_val())])
        #)
    paginator = QuerySetPaginator(qset, paginate_by)
    page_no = request.GET.get('p', paginator.page_range[0])
    try:
        page_no = int(page_no)
        if not page_no in paginator.page_range:
            page_no = paginator.page_range[0]
    except Exception:
        page_no = paginator.page_range[0]
    context = {}
    page = paginator.page(page_no)
    objs = page.object_list
    # make objs viewed by user TODO presunout nasledujici podminku nekam jinam
    if not isinstance(request.user, AnonymousUser):
        CT = ContentType.objects.get_for_model(Comment)
        for item in objs:
            pv = PostViewed.objects.filter(target_ct=CT, target_id=item._get_pk_val(), user=request.user)
            if pv:
                continue
            post_viewed = PostViewed(target_ct=CT, target_id=item._get_pk_val(), user=request.user)
            post_viewed.save()
    context['object_list'] = objs
    context.update({
        'is_paginated': paginator.num_pages > 1,
        'results_per_page': paginate_by,
        'page': page,
})
    return context

def get_category_topics_url(category):
    # category.slug/year/_(topics)
    return '/%s/%s/%s/' % (category.slug, slugify(_('static')), slugify(_('topics')))

def process_login(request, login_data):
    usr = authenticate(username=login_data['username'], password=login_data['password'])
    if not usr:
        return STATE_BAD_LOGIN_OR_PASSWORD
    if not usr.is_active:
        return STATE_NOT_ACTIVE
    login(request, usr)
    return STATE_OK #user is logged in

def filter_banned_strings(content):
    REPLACEMENT = '***'
    out = content
    for item in BannedString.objects.values():
        if item['isregexp']:
            out = re.sub(word, REPLACEMENT, out)
            continue
        word = item['expression']
        position = out.find(word)
        while position > -1:
            poslen = len(item['expression']) + position
            out = out[ :position ] + REPLACEMENT + out[ poslen: ]
            position = out.find(word)
    return out

def view_unread(request):
    """ View all posted things since last login. """
    if not isinstance(request.user, User):
        raise http.Http404('User does not exist!')
    u = request.user
    qset = TopicThread.objects.get_unread_topicthreads(request.user)
    context = Context()
    context.update(paginate_queryset_for_request(request, qset))
    return render_to_response(
        ('page/content_type/discussions.question/unread_threads.html',),
        context,
        context_instance=RequestContext(request)
)

def user_posts(request, username):
    """
    View all posts posted by user with username.
    """
    users = User.objects.filter(username=username)
    if not users:
        raise http.Http404('User does not exist!')
    u = users[0]
    CT = ContentType.objects.get_for_model(TopicThread)
    qset = Comment.objects.filter(target_ct=CT).filter(user=u)
    context = Context()
    context.update(paginate_queryset_for_request(request, qset))
    return render_to_response(
        ('page/content_type/discussions.question/user_posts.html',),
        context,
        context_instance=RequestContext(request)
)

def posts(request, bits, context):

    # TODO !!! REFACTOR !!!
    """ Posts view (list of posts associated to given topic) """
    if not bits:
        raise http.Http404('Unsupported url. Slug of topic-thread needed.')

    frmLogin = LoginForm()
    topic = context['object']
    category = context['category']
    # category.slug/year/_(topics)
    context['topics_url'] = get_category_topics_url(category)
    context['question_form_state'] = STATE_EMPTY
    context['login_form_state'] = STATE_UNAUTHORIZED
    thr = TopicThread.objects.get(slug=bits[0])

    user = get_user(request)
    data = {}

    if user.is_authenticated():
        data['nickname'] = user.username
        data['email'] = user.email

    frm = QuestionForm(data)

    if len(bits) > 1:
        if bits[1] == 'login':
            f = LoginForm(request.POST)
            if f.is_valid():
                state = process_login(request, f.cleaned_data)
                if state == STATE_OK:
                    return http.HttpResponseRedirect(thr.get_absolute_url())
                context['login_form_state'] = state
        elif bits[1] == 'logout':
            logout(request)
            return http.HttpResponseRedirect(thr.get_absolute_url())
        elif bits[1] == 'register':
            return http.HttpResponseRedirect(reverse('registration_register'))
    else:
        # receiving new post in QuestionForm
        if request.POST:
            frm = QuestionForm(request.POST)
            if not frm.is_valid():
                context['question_form_state'] = STATE_INVALID
            elif frm.cleaned_data['content'].strip():
                context['question_form_state'] = STATE_OK

                if user.is_authenticated():
                    add_post(frm.cleaned_data['content'], thr, user=user, ip=get_ip(request))
                else:
                    add_post(frm.cleaned_data['content'], thr, nickname=frm.cleaned_data['nickname'], email=frm.cleaned_data['email'], ip=get_ip(request))

                frm.clean()

            else:
                context['question_form_state'] = STATE_INVALID
        else:
            thr.hit() # increment view counter
    if request.user.is_staff:
        comment_set = comments_on_thread__by_submit_date(thr) # specialized function created because of caching
    else:
        comment_set = comments_on_thread__spec_filter(thr) # specialized function created because of caching

    comment_set = thr.get_posts_by_date()
    thread_url = '%s/' % thr.get_absolute_url()
    context['thread'] = thr
    context['posts'] = comment_set
    context['login_form'] = frmLogin
    context['register_form_url'] = '%sregister/' % thread_url
    context['question_form'] = frm
    context['question_form_action'] = thread_url
    context['login_form_action'] = '%slogin/' % thread_url
    context['logout_form_action'] = '%slogout/' % thread_url
    context.update(paginate_queryset_for_request(request, comment_set))
    tplList = (
        'page/category/%s/content_type/discussions.question/%s/posts.html' % (category.path, topic.slug,),
        'page/category/%s/content_type/discussions.question/posts.html' % (category.path,),
        'page/content_type/discussions.question/posts.html',
)
    return render_to_response(
        tplList,
        context,
        context_instance=RequestContext(request)
)

def create_thread(request, bits, context):
    """ creates new thread (that is new TopciThread and first Comment) """
    topic = context['object']
    frmThread = ThreadForm(request.POST or None)
    context['login_form_state'] = STATE_UNAUTHORIZED
    frmLogin = LoginForm(request.POST or None)
    if frmLogin.is_valid():
        state = process_login(request, frmLogin.cleaned_data)
        if state == STATE_OK:
            url = '%s%s' % (topic.get_absolute_url(), slugify(_('create thread')))
            return http.HttpResponseRedirect(url)
        context['login_form_state'] = state

    if frmThread.is_valid():
        data = frmThread.cleaned_data
        thr = TopicThread(
            title=data['title'],
            slug=slugify(data['title']),
            created=datetime.now(),
            author=get_user(request),
            topic=topic
)
        try:
            thr.save()
            add_post(data['content'], thr, get_user(request), get_ip(request))
            #TODO invalidate cached thread list
            return http.HttpResponseRedirect(topic.get_absolute_url())
        except DuplicationError:
            context['error'] = _('Thread with given title already exist.')
    context['login_form'] = frmLogin
    context['login_form_action'] = '%slogin/' % request.get_full_path()
    context['logout_form_action'] = '%slogout/' % request.get_full_path()
    context['question_form'] = frmThread
    context['question_form_action'] = request.get_full_path()
    category = context['category']
    return render_to_response(
            (
                'page/category/%s/content_type/discussions.question/%s/ask.html' % (category.path, topic.slug,),
                'page/category/%s/content_type/discussions.question/ask.html' % (category.path,),
                'page/content_type/discussions.question/ask.html',
),
            context,
            context_instance=RequestContext(request)
)

def question(request, bits, context):
    log.debug('question() view')
    if not bits:
        raise http.Http404

    topic = context['object']
    category = context['category']
    question = get_cached_object_or_404(Question, topic=topic, slug=bits[0])
    context['topic'] = topic
    context['object'] = question
    context['content_type'] = ContentType.objects.get_for_model(Question)

    if len(bits) > 1 and bits[1] == slugify(_('comments')):
        new_bits = bits[2:]
    else:
        new_bits = bits[1:]
    from ella.comments.urls import comments_custom_urls
    return comments_custom_urls(request, new_bits, context)

def topic(request, context):
    top = context['object']  # topic
    cat = context['category']
    context['topics_url'] = get_category_topics_url(cat)
    slug = top.slug
    log.debug('topic() view')
    # TODO: add caching
    ct = ContentType.objects.get_for_model(Topic)
    t_list = [
                'page/category/%s/content_type/%s.%s/%s/object.html' % (cat.path, ct.app_label, ct.model, slug),
                'page/category/%s/content_type/%s.%s/object.html' % (cat.path, ct.app_label, ct.model),
                'page/category/%s/object.html' % (cat.path),
                'page/content_type/%s.%s/object.html' % (ct.app_label, ct.model),
                'page/object.html',
            ]

    kwargs = {}
    if 'p' in request.GET:
        kwargs['page'] = request.GET['p']
    qset = top.topicthread_set.all()
    #context.update(paginate_queryset_for_request(request, qset))
    return object_list(
            request,
            queryset=qset,
            extra_context=context,
            paginate_by=10,
            template_name=loader.select_template(t_list).name,
            **kwargs
)

log = logging.getLogger('ella.discussions')
