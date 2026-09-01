"""Shared list pagination.

One helper and one template partial so every long admin list pages the same
way, and so switching pages never loses the filters the admin already applied.
"""
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator

DEFAULT_PER_PAGE = 25


def paginate(request, queryset, per_page=DEFAULT_PER_PAGE):
    """Return the requested page of ``queryset``.

    Out-of-range or non-numeric ``?page=`` values fall back to a valid page
    rather than raising — a stale bookmark should still show something useful.
    """
    paginator = Paginator(queryset, per_page)
    try:
        return paginator.page(request.GET.get("page"))
    except PageNotAnInteger:
        return paginator.page(1)
    except EmptyPage:
        return paginator.page(paginator.num_pages)


def querystring_without_page(request):
    """Current query params minus ``page``, ready to append a new page number.

    Keeps search / status / event filters intact when paging.
    """
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"{encoded}&" if encoded else ""
