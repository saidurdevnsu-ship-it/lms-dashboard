from django import template

from learning.dashboard_service import get_dashboard_stats

register = template.Library()


@register.simple_tag(takes_context=True)
def dashboard_stats(context):
    return get_dashboard_stats(context["request"])
