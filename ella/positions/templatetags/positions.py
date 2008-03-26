from django import template
from django.template import TemplateSyntaxError
from django.conf import settings

from ella.utils.templatetags import parse_getforas_triplet
from ella.core.models import Category
from ella.core.cache import get_cached_object
from ella.positions.models import Position


register = template.Library()


@register.tag
def position(parser, token):
    """
    Render a given position for category.
    If some position is not defined for first category, position from its parent
    category is used unless nofallback is specified.

    Syntax::

        {% position POSITION_NAME for CATEGORY [nofallback] %}
        {% position POSITION_NAME for CATEGORY using BOX_TYPE [nofallback] %}

    Example usage::

        {% position top_left for category %}
    """
    bits = token.split_contents()
    nodelist = parser.parse(('end' + bits[0],))
    parser.delete_first_token()

    nofallback = False
    if bits[-1] == 'nofallback':
        nofallback = True
        bits.pop()

    if len(bits) == 4 and bits[2] == 'for':
        pos_name, category = bits[1], bits[3]
        box_type = None
    elif len(bits) == 6 and bits[2] == 'for' and bits[4] == 'using':
        pos_name, category, box_type = bits[1], bits[3], bits[5]
    else:
        raise TemplateSyntaxError, 'Invalid syntax: {% position POSITION_NAME for CATEGORY [nofallback] %}'

    return PositionNode(category, pos_name, nodelist, box_type, nofallback)

class PositionNode(template.Node):
    def __init__(self, category, position, nodelist, box_type, nofallback):
        self.category, self.position = category, position
        self.nodelist, self.box_type = nodelist, box_type
        self.nofallback = nofallback

    def render(self, context):
        try:
            cat = template.resolve_variable(self.category, context)
            if not isinstance(cat, Category):
                cat = get_cached_object(Category, site=settings.SITE_ID, slug=self.category)
        except template.VariableDoesNotExist, Category.DoesNotExist:
            return ''

        try:
            pos = Position.objects.get_active_position(cat, self.position, self.nofallback)
        except Position.DoesNotExist:
            return ''

        return pos.render(context, self.nodelist, self.box_type)


@register.tag
def ifposition(parser, token):
    """
    Syntax::

        {% ifposition POSITION_NAME for CATEGORY [nofallback] %}
        {% else %}
        {% endifposition %}

    """
    bits = list(token.split_contents())
    end_tag = 'end' + bits[0]

    nofallback = False
    if bits[-1] == 'nofallback':
        nofallback = True
        bits.pop()

    if len(bits) == 4 and bits[2] == 'for':
        pos_name, category = bits[1], bits[3]
        box_type = None
    elif len(bits) == 6 and bits[2] == 'for' and bits[4] == 'using':
        pos_name, category, box_type = bits[1], bits[3], bits[5]
    else:
        raise TemplateSyntaxError, 'Invalid syntax: {% position POSITION_NAME for CATEGORY [nofallback] %}'

    nodelist_true = parser.parse(('else', end_tag))
    token = parser.next_token()

    if token.contents == 'else':
        nodelist_false = parser.parse((end_tag,))
        parser.delete_first_token()
    else:
        nodelist_false = template.NodeList()

    return IfPositionNode(category, pos_name, nofallback, nodelist_true, nodelist_false)

class IfPositionNode(template.Node):
    def __init__(self, category, position, nofallback, nodelist_true, nodelist_false):
        self.category, self.position = category, position
        self.nofallback, self.nodelist_true, self.nodelist_false = nofallback, nodelist_true, nodelist_false

    def render(self, context):
        try:
            cat = template.resolve_variable(self.category, context)

            if not isinstance(cat, Category):
                cat = get_cached_object(Category, site=settings.SITE_ID, slug=self.category)

            pos = Position.objects.get_active_position(cat, self.position, self.nofallback)
            return self.nodelist_true.render(context)
        except (Position.DoesNotExist, template.VariableDoesNotExist, Category.DoesNotExist):
            return self.nodelist_false.render(context)

