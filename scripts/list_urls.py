import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.local')
import django
django.setup()
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

def list_patterns(patterns, prefix=''):
    out = []
    for p in patterns:
        if isinstance(p, URLPattern):
            out.append(prefix + str(p.pattern) + ' -> ' + p.callback.__name__)
        elif isinstance(p, URLResolver):
            out += list_patterns(p.url_patterns, prefix + str(p.pattern))
    return out

for line in list_patterns(get_resolver().url_patterns):
    print(line)
