import datetime
from pylons import config
from sqlalchemy.sql import select
import datetime
import ckan.authz
import ckan.model
import ckan.misc
import ckan.logic as logic
import ckan.plugins as plugins
import ckan.lib.helpers as h
import ckan.lib.dictization as d

## package save

def group_list_dictize(obj_list, context,
                       sort_key=lambda x:x['display_name'], reverse=False):

    active = context.get('active', True)
    with_private = context.get('include_private_packages', False)
    result_list = []

    for obj in obj_list:
        if context.get('with_capacity'):
            obj, capacity = obj
            group_dict = d.table_dictize(obj, context, capacity=capacity)
        else:
            group_dict = d.table_dictize(obj, context)
        group_dict.pop('created')
        if active and obj.state not in ('active', 'pending'):
            continue

        group_dict['display_name'] = obj.display_name

        group_dict['packages'] = \
                len(obj.active_packages(with_private=with_private).all())

        if context.get('for_view'):
            for item in plugins.PluginImplementations(
                    plugins.IGroupController):
                group_dict = item.before_view(group_dict)

        result_list.append(group_dict)
    return sorted(result_list, key=sort_key, reverse=reverse)

def resource_list_dictize(res_list, context):

    active = context.get('active', True)
    result_list = []
    for res in res_list:
        resource_dict = resource_dictize(res, context)
        if active and res.state not in ('active', 'pending'):
            continue

        result_list.append(resource_dict)

    return sorted(result_list, key=lambda x: x["position"])

def related_list_dictize(related_list, context):
    result_list = []
    for res in related_list:
        related_dict = related_dictize(res, context)
        result_list.append(related_dict)

    return sorted(result_list, key=lambda x: x["created"], reverse=True)


def extras_dict_dictize(extras_dict, context):
    result_list = []
    for name, extra in extras_dict.iteritems():
        dictized = d.table_dictize(extra, context)
        if not extra.state == 'active':
            continue
        value = dictized["value"]
        ## This is to make sure the frontend does not show a plain string
        ## as json with brackets.
        if not(context.get("extras_as_string") and isinstance(value, basestring)):
            dictized["value"] = h.json.dumps(value)
        result_list.append(dictized)

    return sorted(result_list, key=lambda x: x["key"])

def extras_list_dictize(extras_list, context):
    result_list = []
    active = context.get('active', True)
    for extra in extras_list:
        dictized = d.table_dictize(extra, context)
        if active and extra.state not in ('active', 'pending'):
            continue
        value = dictized["value"]
        if not(context.get("extras_as_string") and isinstance(value, basestring)):
            dictized["value"] = h.json.dumps(value)
        result_list.append(dictized)

    return sorted(result_list, key=lambda x: x["key"])

def resource_dictize(res, context):
    resource = d.table_dictize(res, context)
    extras = resource.pop("extras", None)
    if extras:
        resource.update(extras)
    #tracking
    if not context.get('for_edit'):
        model = context['model']
        tracking = model.TrackingSummary.get_for_resource(res.url)
        resource['tracking_summary'] = tracking
    # some urls do not have the protocol this adds http:// to these
    url = resource['url']
    if not (url.startswith('http://') or url.startswith('https://')):
        resource['url'] = u'http://' + url
    return resource

def related_dictize(rel, context):
    return d.table_dictize(rel, context)

def _execute_with_revision(q, rev_table, context):
    '''
    Takes an SqlAlchemy query (q) that is (at its base) a Select on an
    object revision table (rev_table), and normally it filters to the
    'current' object revision (latest which has been moderated) and
    returns that.

    But you can provide revision_id, revision_date or pending in the
    context and it will filter to an earlier time or the latest unmoderated
    object revision.

    Raises NotFound if context['revision_id'] is provided, but the revision
    ID does not exist.

    Returns [] if there are no results.

    '''
    model = context['model']
    meta = model.meta
    session = model.Session
    revision_id = context.get('revision_id')
    revision_date = context.get('revision_date')
    pending = context.get('pending')

    if revision_id:
        revision = session.query(context['model'].Revision).filter_by(
            id=revision_id).first()
        if not revision:
            raise logic.NotFound
        revision_date = revision.timestamp

    if revision_date:
        q = q.where(rev_table.c.revision_timestamp <= revision_date)
        q = q.where(rev_table.c.expired_timestamp > revision_date)
    elif pending:
        q = q.where(rev_table.c.expired_timestamp == datetime.datetime(9999, 12, 31))
    else:
        q = q.where(rev_table.c.current == True)

    return session.execute(q)


def package_dictize(pkg, context):
    '''
    Given a Package object, returns an equivalent dictionary.

    Normally this is the current revision (most recent moderated version),
    but you can provide revision_id, revision_date or pending in the
    context and it will filter to an earlier time or the latest unmoderated
    object revision.

    May raise NotFound. TODO: understand what the specific set of
    circumstances are that cause this.
    '''
    model = context['model']
    #package
    package_rev = model.package_revision_table
    q = select([package_rev]).where(package_rev.c.id == pkg.id)
    result = _execute_with_revision(q, package_rev, context).first()
    if not result:
        raise logic.NotFound
    result_dict = d.table_dictize(result, context)
    #resources
    res_rev = model.resource_revision_table
    resource_group = model.resource_group_table
    q = select([res_rev], from_obj = res_rev.join(resource_group,
               resource_group.c.id == res_rev.c.resource_group_id))
    q = q.where(resource_group.c.package_id == pkg.id)
    result = _execute_with_revision(q, res_rev, context)
    result_dict["resources"] = resource_list_dictize(result, context)

    #tags
    tag_rev = model.package_tag_revision_table
    tag = model.tag_table
    q = select([tag, tag_rev.c.state, tag_rev.c.revision_timestamp],
        from_obj=tag_rev.join(tag, tag.c.id == tag_rev.c.tag_id)
        ).where(tag_rev.c.package_id == pkg.id)
    result = _execute_with_revision(q, tag_rev, context)
    result_dict["tags"] = d.obj_list_dictize(result, context, lambda x: x["name"])

    # Add display_names to tags. At first a tag's display_name is just the
    # same as its name, but the display_name might get changed later (e.g.
    # translated into another language by the multilingual extension).
    for tag in result_dict['tags']:
        assert not tag.has_key('display_name')
        tag['display_name'] = tag['name']

    #extras
    extra_rev = model.extra_revision_table
    q = select([extra_rev]).where(extra_rev.c.package_id == pkg.id)
    result = _execute_with_revision(q, extra_rev, context)
    result_dict["extras"] = extras_list_dictize(result, context)
    #tracking
    tracking = model.TrackingSummary.get_for_package(pkg.id)
    result_dict['tracking_summary'] = tracking
    #groups
    member_rev = model.member_revision_table
    group = model.group_table
    q = select([group, member_rev.c.capacity],
               from_obj=member_rev.join(group, group.c.id == member_rev.c.group_id)
               ).where(member_rev.c.table_id == pkg.id)\
                .where(member_rev.c.state == 'active')
    result = _execute_with_revision(q, member_rev, context)
    result_dict["groups"] = d.obj_list_dictize(result, context)
    #relations
    rel_rev = model.package_relationship_revision_table
    q = select([rel_rev]).where(rel_rev.c.subject_package_id == pkg.id)
    result = _execute_with_revision(q, rel_rev, context)
    result_dict["relationships_as_subject"] = d.obj_list_dictize(result, context)
    q = select([rel_rev]).where(rel_rev.c.object_package_id == pkg.id)
    result = _execute_with_revision(q, rel_rev, context)
    result_dict["relationships_as_object"] = d.obj_list_dictize(result, context)

    # Extra properties from the domain object
    # We need an actual Package object for this, not a PackageRevision
    if isinstance(pkg, ckan.model.PackageRevision):
        pkg = model.Package.get(pkg.id)

    # isopen
    result_dict['isopen'] = pkg.isopen if isinstance(pkg.isopen,bool) else pkg.isopen()

    # type
    result_dict['type']= pkg.type

    # licence
    if pkg.license and pkg.license.url:
        result_dict['license_url']= pkg.license.url
        result_dict['license_title']= pkg.license.title.split('::')[-1]
    elif pkg.license:
        result_dict['license_title']= pkg.license.title
    else:
        result_dict['license_title']= pkg.license_id

    # creation and modification date
    result_dict['metadata_modified'] = context.pop('metadata_modified')
    result_dict['metadata_created'] = pkg.metadata_created.isoformat() \
        if pkg.metadata_created else None

    if context.get('for_view'):
        for item in plugins.PluginImplementations( plugins.IPackageController):
            result_dict = item.before_view(result_dict)

    return result_dict

def _get_members(context, group, member_type):

    model = context['model']
    Entity = getattr(model, member_type[:-1].capitalize())
    return model.Session.query(Entity, model.Member.capacity).\
               join(model.Member, model.Member.table_id == Entity.id).\
               filter(model.Member.group_id == group.id).\
               filter(model.Member.state == 'active').\
               filter(model.Member.table_name == member_type[:-1]).all()

def group_dictize(group, context):
    model = context['model']
    result_dict = d.table_dictize(group, context)

    result_dict['display_name'] = group.display_name

    result_dict['extras'] = extras_dict_dictize(
        group._extras, context)

    context['with_capacity'] = True

    result_dict['packages'] = d.obj_list_dictize(
        _get_members(context, group, 'packages'),
        context)

    result_dict['tags'] = tag_list_dictize(
        _get_members(context, group, 'tags'),
        context)

    result_dict['groups'] = group_list_dictize(
        _get_members(context, group, 'groups'),
        context)

    result_dict['users'] = user_list_dictize(
        _get_members(context, group, 'users'),
        context)

    context['with_capacity'] = False

    if context.get('for_view'):
        for item in plugins.PluginImplementations(plugins.IGroupController):
            result_dict = item.before_view(result_dict)

    return result_dict

def tag_list_dictize(tag_list, context):

    result_list = []
    for tag in tag_list:
        if context.get('with_capacity'):
            tag, capacity = tag
            dictized = d.table_dictize(tag, context, capacity=capacity)
        else:
            dictized = d.table_dictize(tag, context)

        # Add display_names to tag dicts. At first a tag's display_name is just
        # the same as its name, but the display_name might get changed later
        # (e.g.  translated into another language by the multilingual
        # extension).
        assert not dictized.has_key('display_name')
        dictized['display_name'] = dictized['name']

        if context.get('for_view'):
            for item in plugins.PluginImplementations(
                    plugins.ITagController):
                dictized = item.before_view(dictized)

        result_list.append(dictized)

    return result_list

def tag_dictize(tag, context):

    result_dict = d.table_dictize(tag, context)
    result_dict["packages"] = d.obj_list_dictize(tag.packages, context)

    # Add display_names to tags. At first a tag's display_name is just the
    # same as its name, but the display_name might get changed later (e.g.
    # translated into another language by the multilingual extension).
    assert not result_dict.has_key('display_name')
    result_dict['display_name'] = result_dict['name']

    if context.get('for_view'):
        for item in plugins.PluginImplementations(
                plugins.ITagController):
            result_dict = item.before_view(result_dict)

    return result_dict

def user_list_dictize(obj_list, context,
                      sort_key=lambda x:x['name'], reverse=False):

    result_list = []

    for obj in obj_list:
        user_dict = user_dictize(obj, context)
        result_list.append(user_dict)
    return sorted(result_list, key=sort_key, reverse=reverse)

def member_dictize(member, context):
    return d.table_dictize(member, context)

def user_dictize(user, context):

    if context.get('with_capacity'):
        user, capacity = user
        result_dict = d.table_dictize(user, context, capacity=capacity)
    else:
        result_dict = d.table_dictize(user, context)

    del result_dict['password']

    result_dict['display_name'] = user.display_name
    result_dict['email_hash'] = user.email_hash
    result_dict['number_of_edits'] = user.number_of_edits()
    result_dict['number_administered_packages'] = user.number_administered_packages()

    requester = context['user']

    if not (ckan.authz.Authorizer().is_sysadmin(unicode(requester)) or
            requester == user.name or
            context.get('keep_sensitive_data', False)):
        # If not sysadmin or the same user, strip sensible info
        result_dict.pop('apikey', None)
        result_dict.pop('reset_key', None)
        result_dict.pop('email', None)

    model = context['model']
    session = model.Session

    if context.get('with_related'):
        related_items = session.query(model.Related).\
                        filter(model.Related.owner_id==user.id).all()
        result_dict['related_items'] = related_list_dictize(related_items,
                                                            context)

    return result_dict

def task_status_dictize(task_status, context):
    return d.table_dictize(task_status, context)

## conversion to api

def group_to_api(group, context):
    api_version = context.get('api_version')
    assert api_version, 'No api_version supplied in context'
    dictized = group_dictize(group, context)
    dictized["extras"] = dict((extra["key"], h.json.loads(extra["value"]))
                              for extra in dictized["extras"])
    if api_version == 1:
        dictized["packages"] = sorted([pkg["name"] for pkg in dictized["packages"]])
    else:
        dictized["packages"] = sorted([pkg["id"] for pkg in dictized["packages"]])
    return dictized

def tag_to_api(tag, context):
    api_version = context.get('api_version')
    assert api_version, 'No api_version supplied in context'
    dictized = tag_dictize(tag, context)
    if api_version == 1:
        return sorted([package["name"] for package in dictized["packages"]])
    else:
        return sorted([package["id"] for package in dictized["packages"]])


def resource_dict_to_api(res_dict, package_id, context):
    res_dict.pop("revision_id")
    res_dict.pop("state")
    res_dict.pop("revision_timestamp")
    res_dict["package_id"] = package_id


def package_to_api(pkg, context):
    api_version = context.get('api_version')
    assert api_version, 'No api_version supplied in context'
    dictized = package_dictize(pkg, context)
    dictized.pop("revision_timestamp")

    dictized["tags"] = [tag["name"] for tag in dictized["tags"] \
                        if not tag.get('vocabulary_id')]
    dictized["extras"] = dict((extra["key"], h.json.loads(extra["value"]))
                              for extra in dictized["extras"])
    dictized['license'] = pkg.license.title if pkg.license else None
    dictized['ratings_average'] = pkg.get_average_rating()
    dictized['ratings_count'] = len(pkg.ratings)
    dictized['notes_rendered'] = ckan.misc.MarkdownFormat().to_html(pkg.notes)

    site_url = config.get('ckan.site_url', None)
    if site_url:
        dictized['ckan_url'] = '%s/dataset/%s' % (site_url, pkg.name)

    for resource in dictized["resources"]:
        resource_dict_to_api(resource, pkg.id, context)

    def make_api_1(package_id):
        return pkg.get(package_id).name

    def make_api_2(package_id):
        return package_id

    if api_version == 1:
        api_fn = make_api_1
        dictized["groups"] = [group["name"] for group in dictized["groups"]]
        # FIXME why is this just for version 1?
        if pkg.resources:
            dictized['download_url'] = pkg.resources[0].url
    else:
        api_fn = make_api_2
        dictized["groups"] = [group["id"] for group in dictized["groups"]]

    subjects = dictized.pop("relationships_as_subject")
    objects = dictized.pop("relationships_as_object")

    relationships = []
    for rel in objects:
        model = context['model']
        swap_types = model.PackageRelationship.forward_to_reverse_type
        type = swap_types(rel['type'])
        relationships.append({'subject': api_fn(rel['object_package_id']),
                              'type': type,
                              'object': api_fn(rel['subject_package_id']),
                              'comment': rel["comment"]})
    for rel in subjects:
        relationships.append({'subject': api_fn(rel['subject_package_id']),
                              'type': rel['type'],
                              'object': api_fn(rel['object_package_id']),
                              'comment': rel["comment"]})

    dictized['relationships'] = relationships

    return dictized

def vocabulary_dictize(vocabulary, context):
    vocabulary_dict = d.table_dictize(vocabulary, context)
    assert not vocabulary_dict.has_key('tags')
    vocabulary_dict['tags'] = [tag_dictize(tag, context) for tag
            in vocabulary.tags]
    return vocabulary_dict

def vocabulary_list_dictize(vocabulary_list, context):
    return [vocabulary_dictize(vocabulary, context)
            for vocabulary in vocabulary_list]

def activity_dictize(activity, context):
    activity_dict = d.table_dictize(activity, context)
    return activity_dict

def activity_list_dictize(activity_list, context):
    return [activity_dictize(activity, context) for activity in activity_list]

def activity_detail_dictize(activity_detail, context):
    return d.table_dictize(activity_detail, context)

def activity_detail_list_dictize(activity_detail_list, context):
    return [activity_detail_dictize(activity_detail, context)
            for activity_detail in activity_detail_list]


def package_to_api1(pkg, context):
    # DEPRICIATED set api_version in context and use package_to_api()
    context['api_version'] = 1
    return package_to_api(pkg, context)

def package_to_api2(pkg, context):
    # DEPRICIATED set api_version in context and use package_to_api()
    context['api_version'] = 2
    return package_to_api(pkg, context)

def group_to_api1(group, context):
    # DEPRICIATED set api_version in context and use group_to_api()
    context['api_version'] = 1
    return group_to_api(group, context)

def group_to_api2(group, context):
    # DEPRICIATED set api_version in context and use group_to_api()
    context['api_version'] = 2
    return group_to_api(group, context)

def tag_to_api1(tag, context):
    # DEPRICIATED set api_version in context and use tag_to_api()
    context['api_version'] = 1
    return tag_to_api(tag, context)

def tag_to_api2(tag, context):
    # DEPRICIATED set api_version in context and use tag_to_api()
    context['api_version'] = 2
    return tag_to_api(tag, context)

def user_following_user_dictize(follower, context):
    return d.table_dictize(follower, context)

def user_following_dataset_dictize(follower, context):
    return d.table_dictize(follower, context)
