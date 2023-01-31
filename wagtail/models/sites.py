from collections import namedtuple

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import (
    _user_get_permissions,
    _user_has_perm,
    _user_has_module_perms,
)
from django.core.cache import cache
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.db import models
from django.db.models import Case, IntegerField, Q, When, UniqueConstraint
from django.db.models.functions import Lower
from django.http.request import split_domain_port, HttpRequest
from django.utils.itercompat import is_iterable
from django.utils.translation import gettext_lazy as _

SiteUser = None

MATCH_HOSTNAME_PORT = 0
MATCH_HOSTNAME_DEFAULT = 1
MATCH_DEFAULT = 2
MATCH_HOSTNAME = 3


def get_site_for_hostname(hostname, port):
    """Return the wagtailcore.Site object for the given hostname and port."""
    Site = apps.get_model("wagtailcore.Site")

    sites = list(
        Site.objects.annotate(
            match=Case(
                # annotate the results by best choice descending
                # put exact hostname+port match first
                When(hostname=hostname, port=port, then=MATCH_HOSTNAME_PORT),
                # then put hostname+default (better than just hostname or just default)
                When(
                    hostname=hostname, is_default_site=True, then=MATCH_HOSTNAME_DEFAULT
                ),
                # then match default with different hostname. there is only ever
                # one default, so order it above (possibly multiple) hostname
                # matches so we can use sites[0] below to access it
                When(is_default_site=True, then=MATCH_DEFAULT),
                # because of the filter below, if it's not default then its a hostname match
                default=MATCH_HOSTNAME,
                output_field=IntegerField(),
            )
        )
        .filter(Q(hostname=hostname) | Q(is_default_site=True))
        .order_by("match")
        .select_related("root_page")
    )

    if sites:
        # if there's a unique match or hostname (with port or default) match
        if len(sites) == 1 or sites[0].match in (
            MATCH_HOSTNAME_PORT,
            MATCH_HOSTNAME_DEFAULT,
        ):
            return sites[0]

        # if there is a default match with a different hostname, see if
        # there are many hostname matches. if only 1 then use that instead
        # otherwise we use the default
        if sites[0].match == MATCH_DEFAULT:
            return sites[len(sites) == 2]

    raise Site.DoesNotExist()


class SiteManager(models.Manager):
    def get_queryset(self):
        return super(SiteManager, self).get_queryset().order_by(Lower("hostname"))

    def get_by_natural_key(self, hostname, port):
        return self.get(hostname=hostname, port=port)


SiteRootPath = namedtuple("SiteRootPath", "site_id root_path root_url language_code")


class Site(models.Model):
    hostname = models.CharField(
        verbose_name=_("hostname"), max_length=255, db_index=True
    )
    port = models.IntegerField(
        verbose_name=_("port"),
        default=80,
        help_text=_(
            "Set this to something other than 80 if you need a specific port number to appear in URLs"
            " (e.g. development on port 8000). Does not affect request handling (so port forwarding still works)."
        ),
    )
    site_name = models.CharField(
        verbose_name=_("site name"),
        max_length=255,
        blank=True,
        help_text=_("Human-readable name for the site."),
    )
    root_page = models.ForeignKey(
        "Page",
        verbose_name=_("root page"),
        related_name="sites_rooted_here",
        on_delete=models.CASCADE,
    )
    is_default_site = models.BooleanField(
        verbose_name=_("is default site"),
        default=False,
        help_text=_(
            "If true, this site will handle requests for all other hostnames that do not have a site entry of their own"
        ),
    )

    objects = SiteManager()

    class Meta:
        unique_together = ("hostname", "port")
        verbose_name = _("site")
        verbose_name_plural = _("sites")

    def natural_key(self):
        return (self.hostname, self.port)

    def __str__(self):
        default_suffix = " [{}]".format(_("default"))
        if self.site_name:
            return self.site_name + (default_suffix if self.is_default_site else "")
        else:
            return (
                self.hostname
                + ("" if self.port == 80 else (":%d" % self.port))
                + (default_suffix if self.is_default_site else "")
            )

    @staticmethod
    def find_for_request(request):
        """
        Find the site object responsible for responding to this HTTP
        request object. Try:

        * unique hostname first
        * then hostname and port
        * if there is no matching hostname at all, or no matching
          hostname:port combination, fall back to the unique default site,
          or raise an exception

        NB this means that high-numbered ports on an extant hostname may
        still be routed to a different hostname which is set as the default

        The site will be cached via request._wagtail_site
        """

        if request is None:
            return None

        if not hasattr(request, "_wagtail_site"):
            site = Site._find_for_request(request)
            setattr(request, "_wagtail_site", site)
        return request._wagtail_site

    @staticmethod
    def _find_for_request(request):
        hostname = split_domain_port(request.get_host())[0]
        port = request.get_port()
        site = None
        try:
            site = get_site_for_hostname(hostname, port)
        except Site.DoesNotExist:
            pass
            # copy old SiteMiddleware behaviour
        return site

    @property
    def root_url(self):
        if self.port == 80:
            return "http://%s" % self.hostname
        elif self.port == 443:
            return "https://%s" % self.hostname
        else:
            return "http://%s:%d" % (self.hostname, self.port)

    def clean_fields(self, exclude=None):
        super().clean_fields(exclude)
        # Only one site can have the is_default_site flag set
        try:
            default = Site.objects.get(is_default_site=True)
        except Site.DoesNotExist:
            pass
        except Site.MultipleObjectsReturned:
            raise
        else:
            if self.is_default_site and self.pk != default.pk:
                raise ValidationError(
                    {
                        "is_default_site": [
                            _(
                                "%(hostname)s is already configured as the default site."
                                " You must unset that before you can save this site as default."
                            )
                            % {"hostname": default.hostname}
                        ]
                    }
                )

    @staticmethod
    def get_site_root_paths():
        """
        Return a list of `SiteRootPath` instances, most specific path
        first - used to translate url_paths into actual URLs with hostnames

        Each root path is an instance of the `SiteRootPath` named tuple,
        and have the following attributes:

        - `site_id` - The ID of the Site record
        - `root_path` - The internal URL path of the site's home page (for example '/home/')
        - `root_url` - The scheme/domain name of the site (for example 'https://www.example.com/')
        - `language_code` - The language code of the site (for example 'en')
        """
        result = cache.get("wagtail_site_root_paths")

        # Wagtail 2.11 changed the way site root paths were stored. This can cause an upgraded 2.11
        # site to break when loading cached site root paths that were cached with 2.10.2 or older
        # versions of Wagtail. The line below checks if the any of the cached site urls is consistent
        # with an older version of Wagtail and invalidates the cache.
        if result is None or any(len(site_record) == 3 for site_record in result):
            result = []

            for site in Site.objects.select_related(
                "root_page", "root_page__locale"
            ).order_by("-root_page__url_path", "-is_default_site", "hostname"):
                if getattr(settings, "WAGTAIL_I18N_ENABLED", False):
                    result.extend(
                        [
                            SiteRootPath(
                                site.id,
                                root_page.url_path,
                                site.root_url,
                                root_page.locale.language_code,
                            )
                            for root_page in site.root_page.get_translations(
                                inclusive=True
                            ).select_related("locale")
                        ]
                    )
                else:
                    result.append(
                        SiteRootPath(
                            site.id,
                            site.root_page.url_path,
                            site.root_url,
                            site.root_page.locale.language_code,
                        )
                    )

            cache.set("wagtail_site_root_paths", result, 3600)

        return result


class SiteGroupManager(models.Manager):
    use_in_migrations = True

    def get_by_natural_key(self, name, site_id):
        return self.get(name=name, site_id=site_id)


class SiteGroup(models.Model):
    """
    SiteGroup is a generic way of categorizing site_users to apply permissions, or
    some other label, to those users.
    """

    name = models.CharField(_("name"), max_length=150)
    permissions = models.ManyToManyField(
        "auth.Permission",
        related_name="permission_sitegroups",
        verbose_name=_("permissions"),
        blank=True,
    )
    site = models.ForeignKey(
        Site, related_name="site_sitegroups", on_delete=models.CASCADE
    )

    objects = SiteGroupManager()

    class Meta:
        verbose_name = _("group")
        verbose_name_plural = _("groups")

        constraints = [
            UniqueConstraint(fields=["name", "site"], name="unique_name_site")
        ]

    def __str__(self):
        return self.name

    def natural_key(self):
        return self.name, self.site_id


class AbstractSiteUser(models.Model):
    site = models.ForeignKey(
        Site, related_name="site_siteusers", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="user_siteusers",
        on_delete=models.CASCADE,
    )

    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Designates whether this user should be treated as active. "
            "Unselect this instead of deleting accounts."
        ),
    )

    is_superuser = models.BooleanField(
        _("superuser status"),
        default=False,
        help_text=_(
            "Designates that this user has all permissions without "
            "explicitly assigning them."
        ),
    )

    groups = models.ManyToManyField(
        SiteGroup,
        verbose_name=_("groups"),
        blank=True,
        help_text=_(
            "The groups this user belongs to. A user will get all permissions "
            "granted to each of their groups."
        ),
        related_name="sitegroup_siteusers",
    )
    site_user_permissions = models.ManyToManyField(
        "auth.Permission",
        verbose_name=_("user permissions"),
        blank=True,
        help_text=_("Specific permissions for this user."),
        related_name="permission_siteusers",
    )

    class Meta:
        swappable = "SITE_USER_MODEL"
        abstract = True
        constraints = [
            UniqueConstraint(fields=["site", "user"], name="unique_site_user")
        ]

    @staticmethod
    def find_for_request(request: HttpRequest):
        """
        Find the site user object responsible for responding to this HTTP
        request object. Try:

        * reading site_id from session first
        * then get user default choice
        then validate the access to that

        The site user will be cached via request.user.site_user
        """
        from ..sites.utils import set_current_session_project

        global SiteUser
        SiteUser = SiteUser or get_site_user_model()

        site_id = request.session.get("site_id")
        if not site_id:
            # To keep the user working on whatever site they want
            site_id = request.session[
                "site_id"
            ] = request.user.user_siteusers.last().site_id
        site_user = (
            SiteUser.objects.filter(site_id=site_id, user_id=request.user.id)
            .select_related("site")
            .get()
        )
        if (
            not getattr(request.user, "site_user", None)
            or request.user.site_user.site_id != site_id
        ):
            set_current_session_project(request, site_user)
        return request.user.site_user

    def get_site_user_permissions(self, obj=None):
        """
        Return a list of permission strings that this user has directly.
        Query all available auth backends. If an object is passed in,
        return only permissions matching this object.
        """
        return _user_get_permissions(self, obj, "user")

    def get_site_group_permissions(self, obj=None):
        """
        Return a list of permission strings that this user has through their
        groups. Query all available auth backends. If an object is passed in,
        return only permissions matching this object.
        """
        return _user_get_permissions(self, obj, "group")

    def get_all_permissions(self, obj=None):
        return _user_get_permissions(self, obj, "all")

    def has_perm(self, perm, obj=None):
        """
        Return True if the user has the specified permission. Query all
        available auth backends, but return immediately if any backend returns
        True. Thus, a user who has permission from a single auth backend is
        assumed to have permission in general. If an object is provided, check
        permissions for that object.
        """
        # Active superusers have all permissions.
        if self.is_active and self.is_superuser:
            return True

        # Otherwise we need to check the backends.
        return _user_has_perm(self, perm, obj)

    def has_perms(self, perm_list, obj=None):
        """
        Return True if the user has each of the specified permissions. If
        object is passed, check if the user has all required perms for it.
        """
        if not is_iterable(perm_list) or isinstance(perm_list, str):
            raise ValueError("perm_list must be an iterable of permissions.")
        return all(self.has_perm(perm, obj) for perm in perm_list)

    def has_module_perms(self, app_label):
        """
        Return True if the user has any permissions in the given app label.
        Use similar logic as has_perm(), above.
        """
        # Active superusers have all permissions.
        if self.is_active and self.is_superuser:
            return True

        return _user_has_module_perms(self, app_label)


def get_site_user_model() -> AbstractSiteUser:
    """
    Get the site user model from the ``SITE_USER_MODEL`` setting.
    """
    from django.apps import apps

    try:
        return apps.get_model(settings.SITE_USER_MODEL, require_ready=False)
    except ValueError:
        raise ImproperlyConfigured(
            "settings must be of the form 'app_label.model_name'"
        )
    except LookupError:
        raise ImproperlyConfigured(
            "settings refers to model '%s' that has not been installed"
            % settings.SITE_USER_MODEL
        )
    except AttributeError:
        raise ImproperlyConfigured(
            "Please configure settings.SITE_USER_MODEL \
            that should subclass the AbstractSiteUser"
        )
