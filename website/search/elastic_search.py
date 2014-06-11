# -*- coding: utf-8 -*-

import logging
import pyelasticsearch

from website import settings
from website.filters import gravatar
from website.models import User, Node

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


try:
    elastic = pyelasticsearch.ElasticSearch(settings.ELASTIC_URI)
    logging.getLogger('pyelasticsearch').setLevel(logging.DEBUG)
    logging.getLogger('requests').setLevel(logging.DEBUG)
    elastic.health()
except pyelasticsearch.exceptions.ConnectionError as e:
    logger.error(e)
    logger.warn("The SEARCH_ENGINE setting is set to 'elastic', but there "
                "was a problem starting the elasticsearch interface. Is "
                "elasticsearch running?")
    elastic = None


def search(raw_query, start=0):
    orig_query = raw_query

    query, filtered_query = _build_query(raw_query, start)

    counts = {
        'users': elastic.count(filtered_query, index='website', doc_type='user')['count'],
        'projects': elastic.count(filtered_query, index='website', doc_type='project')['count'],
        'components': elastic.count(filtered_query, index='website', doc_type='component')['count'],
        'registrations': elastic.count(filtered_query, index='website', doc_type='registration')['count']
    }

    if 'user:' in orig_query:
        counts['total'] = counts['users']
    elif 'project:' in orig_query:
        counts['total'] = counts['projects']
    elif 'component:' in orig_query:
        counts['total'] = counts['components']
    elif 'registration:' in orig_query:
        counts['total'] = counts['registrations']
    else:
        counts['total'] = sum([x for x in counts.values()])

    raw_results = elastic.search(query, index='website')
    results = [hit['_source'] for hit in raw_results['hits']['hits']]
    formatted_results, tags = create_result(results, counts)

    return formatted_results, tags, counts


def _build_query(raw_query, start=0):

    # Default to searching all types
    type_filter = {
        'or': [
            {
                'type': {'value': 'project'}
            },
            {
                'type': {'value': 'component'}
            },
            {
                'type': {'value': 'user'}
            },
            {
                'type': {'value': 'registration'}
            }
        ]
    }
    raw_query = raw_query.replace('AND', ' ')

    # TODO(fabianvf): Definitely a more elegant way to do this
    if 'project:' in raw_query:
        type_filter = {
            'type': {
                'value': 'project'
            }
        }
    elif 'component:' in raw_query:
        type_filter = {
            'type': {
                'value': 'component'
            }
        }
    elif 'user:' in raw_query:
        type_filter = {
            'type': {
                'value': 'user'
            }
        }
    elif 'registration:' in raw_query:
        type_filter = {
            'type': {
                'value': 'registration'
            }
        }

    raw_query = raw_query.replace('user:', '')
    raw_query = raw_query.replace('project:', '')
    raw_query = raw_query.replace('component:', '')
    raw_query = raw_query.replace('registration:', '')
    raw_query = raw_query.replace('(', '')
    raw_query = raw_query.replace(')', '')
    raw_query = raw_query.replace('\\', '')
    raw_query = raw_query.replace('"', '')

    # If the search contains wildcards, make them mean something
    if '*' in raw_query:
        inner_query = {
            'query_string': {
                'default_field': '_all',
                'query': raw_query,
                'analyze_wildcard': True,
            }
        }
    else:
        inner_query = {
            'match': {
                '_all': raw_query
            }
        }

    # This is the complete query
    query = {
        'query': {
            'function_score': {
                'query': {
                    'filtered': {
                        'filter': type_filter,
                        'query': inner_query
                    }
                },
                'functions': [{
                    'field_value_factor': {
                        'field': 'boost'
                    }
                }],
                'score_mode': 'multiply'
            }
        },
        'from': start,
        'size': 10,
    }

    return query, raw_query


def update_node(node):
    from website.addons.wiki.model import NodeWikiPage

    if node.category == 'project':
        elastic_document_id = node._id
        parent_id = None
        category = 'registration' if node.is_registration else 'project'
    else:
        try:
            elastic_document_id = node._id
            parent_id = node.parent_id
            category = 'registration' if node.is_registration else 'component'
        except IndexError:
            # Skip orphaned components
            return
    if node.is_deleted or not node.is_public:
        delete_doc(elastic_document_id, node)
    else:
        elastic_document = {
            'id': elastic_document_id,
            'contributors': [
                x.fullname for x in node.contributors
                if x is not None
            ],
            'contributors_url': [
                x.profile_url for x in node.contributors
                if x is not None
            ],
            'title': node.title,
            'category': node.category,
            'public': node.is_public,
            'tags': [x._id for x in node.tags],
            'description': node.description,
            'url': node.url,
            'registeredproject': node.is_registration,
            'wikis': {},
            'parent_id': parent_id,
            'boost': int(not node.is_registration) + 1,  # This is for making registered projects less relevant
        }
        for wiki in [
            NodeWikiPage.load(x)
            for x in node.wiki_pages_current.values()
        ]:
            elastic_document['wikis'][wiki.page_name] = wiki.raw_text

        try:
            elastic.update('website', category, id=elastic_document_id, doc=elastic_document, upsert=elastic_document, refresh=True)
        except pyelasticsearch.exceptions.ElasticHttpNotFoundError:
            elastic.index('website', category, elastic_document, id=elastic_document_id, overwrite_existing=True, refresh=True)


def update_user(user):

    user_doc = {
        'id': user._id,
        'user': user.fullname,
        'boost': 2,  # TODO(fabianvf): Probably should make this a constant or something
    }

    try:
        elastic.update('website', 'user', doc=user_doc, id=user._id, upsert=user_doc, refresh=True)
    except pyelasticsearch.exceptions.ElasticHttpNotFoundError:
        elastic.index("website", "user", user_doc, id=user._id, overwrite_existing=True, refresh=True)


def delete_all():
    try:
        elastic.delete_index('website')
    except pyelasticsearch.exceptions.ElasticHttpNotFoundError as e:
        logger.error(e)
        logger.error("The index 'website' was not deleted from elasticsearch")


def delete_doc(elastic_document_id, node):
    category = node.project_or_component
    try:
        elastic.delete('website', category, elastic_document_id, refresh=True)
    except pyelasticsearch.exceptions.ElasticHttpNotFoundError:
        logger.warn("Document with id {} not found in database".format(elastic_document_id))


def create_result(results, counts):
    ''' Takes a dict of counts by type, and a list of dicts of the following structure:
    {
        'category': {NODE CATEGORY},
        'description': {NODE DESCRIPTION},
        'contributors': [{LIST OF CONTRIBUTORS}],
        'title': {TITLE TEXT},
        'url': {URL FOR NODE},
        'tags': {LIST OF TAGS},
        'contributors_url': [{LIST OF LINKS TO CONTRIBUTOR PAGES}],
        'id': {NODE ID},
        'parent_id': {PARENT NODE ID},
        'parent_title': {TITLE TEXT OF PARENT NODE},
        'wikis': {LIST OF WIKIS AND THEIR TEXT},
        'public': {TRUE OR FALSE},
        'registeredproject': {TRUE OR FALSE}
    }

    Returns list of dicts of the following structure:
    {
        'contributors': [{LIST OF CONTRIBUTORS}],
        'wiki_link': '{LINK TO WIKIS}',
        'title': '{TITLE TEXT}',
        'url': '{URL FOR NODE}',
        'nest': {Nested node attributes},
        'tags': [{LIST OF TAGS}],
        'contributors_url': [{LIST OF LINKS TO CONTRIBUTOR PAGES}],
        'is_registration': {TRUE OR FALSE},
        'highlight': [{No longer used, need to phase out}]
        'description': {PROJECT DESCRIPTION}
    }
    '''
    formatted_results = []
    word_cloud = {}
    for result in results:
        # User results are handled specially
        if 'user' in result:
            formatted_results.append({
                'id': result['id'],
                'user': result['user'],
                'user_url': '/profile/' + result['id'],
            })
        else:
            # Build up word cloud
            for tag in result['tags']:
                word_cloud[tag] = 1 if word_cloud.get(tag) is None \
                    else word_cloud[tag] + 1

            # Ensures that information from private projects is never returned
            parent = Node.load(result['parent_id'])
            if parent is not None:
                if parent.is_public:
                    parent_title = parent.title
                    parent_url = parent.url
                    parent_wiki_url = parent.url + 'wiki/'
                    parent_contributors = [
                        contributor.fullname
                        for contributor in parent.contributors
                    ]
                    parent_tags = [tag._id for tag in parent.tags]
                    parent_contributors_url = [
                        contributor.url
                        for contributor in parent.contributors
                    ]
                    parent_is_registration = parent.is_registration
                    parent_description = parent.description
                else:
                    parent_title = '-- private project --'
                    parent_url = ''
                    parent_wiki_url = ''
                    parent_contributors = []
                    parent_tags = []
                    parent_contributors_url = []
                    parent_is_registration = None
                    parent_description = ''

            # Format dictionary for output
            formatted_results.append({
                'contributors': result['contributors'] if parent is None
                    else parent_contributors,
                'wiki_link': result['url'] + 'wiki/' if parent is None
                    else parent_wiki_url,
                'title': result['title'] if parent is None
                    else parent_title,
                'url': result['url'] if parent is None else parent_url,
                'nest': {
                    result['id']:{#Nested components have all their own attributes
                        'title': result['title'],
                        'url': result['url'],
                        'wiki_link': result['url'] + 'wiki/',
                        'contributors': result['contributors'],
                        'contributors_url': result['contributors_url'],
                        'highlight': [],
                        'description': result['description'],
                    }
                } if parent is not None else {},
                'tags': result['tags'] if parent is None else parent_tags,
                'contributors_url': result['contributors_url'] if parent is None
                    else parent_contributors_url,
                'is_registration': result['registeredproject'] if parent is None
                    else parent_is_registration,
                'highlight': [],
                'description': result['description'] if parent is None
                    else parent_description,
            })

    return formatted_results, word_cloud


def search_contributor(query, exclude=None):
    """Search for contributors to add to a project using elastic search. Request must
    include JSON data with a "query" field.

    :param: Search query
    :return: List of dictionaries, each containing the ID, full name, and
        gravatar URL of an OSF user

    """
    import re
    query.replace(" ", "_")
    query = re.sub(r'[\-\+]', '', query)
    query = re.split(r'\s+', query)

    if len(query) > 1:
        and_filter = {'and': []}
        for item in query:
            and_filter['and'].append({
                'prefix': {
                    'user': item.lower()
                }
            })
    else:
        and_filter = {
            'prefix': {
                'user': query[0].lower()
            }
        }

    query = {
        'query': {
            'filtered': {
                'filter': and_filter
            }
        }
    }

    results = elastic.search(query, index='website')
    docs = [hit['_source'] for hit in results['hits']['hits']]

    if exclude:
        docs = (x for x in docs if x.get('id') not in exclude)

    users = []
    for doc in docs:
        # TODO: use utils.serialize_user
        user = User.load(doc['id'])
        if user is None:
            logger.error('Could not load user {0}'.format(doc['id']))
            continue
        if user.is_active():  # exclude merged, unregistered, etc.
            users.append({
                'fullname': doc['user'],
                'id': doc['id'],
                'gravatar_url': gravatar(
                    user,
                    use_ssl=True,
                    size=settings.GRAVATAR_SIZE_ADD_CONTRIBUTOR,
                ),
                'registered': user.is_registered,
                'active': user.is_active()
            })

    return {'users': users}
