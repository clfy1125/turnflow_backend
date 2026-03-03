import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.local')
import django
django.setup()
from django.urls import resolve

path = '/api/v1/integrations/spam-filters/ig-connections/de4a911b-3c56-4daf-886b-ab20d5ef2244/'
try:
    match = resolve(path)
    print('Resolved:', match)
    func = match.func
    print('func:', func)
    print('func name:', getattr(func, '__name__', None))
    print('callable repr:', repr(func))
    print('has attribute view_class:', hasattr(func, 'view_class'))
    print('view_class:', getattr(func, 'view_class', None))
    print('has attribute cls:', hasattr(func, 'cls'))
    print('cls:', getattr(func, 'cls', None))
    print('kwargs:', match.kwargs)
except Exception as e:
    print('Error resolving:', e)
