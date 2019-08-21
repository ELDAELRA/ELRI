from __future__ import unicode_literals

import gzip
import os
import re
import string

from difflib import SequenceMatcher

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.utils import lru_cache
from django.utils._os import upath
from django.utils.encoding import force_text
from django.utils.html import format_html, format_html_join
from django.utils.module_loading import import_string
from django.utils.six import string_types
from django.utils.translation import ugettext_lazy as _, ungettext_lazy


DEFAULT_AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'metashare.accounts.django_password_validators.UserAttributeSimilarityValidator',
        'OPTIONS': {
            'max_similarity': .5,
        }
    }, {
        'NAME': 'metashare.accounts.django_password_validators.MinimumLengthValidator',
        'OPTIONS': {
            'min_length': 10,
        }
    }, {
        'NAME': 'metashare.accounts.django_password_validators.CommonPasswordValidator',
    }, {
        'NAME': 'metashare.accounts.django_password_validators.NumericPasswordValidator',
    }, {
        'NAME': 'metashare.accounts.django_password_validators.AtLeastOneDigitValidator',
    }, {
        'NAME': 'metashare.accounts.django_password_validators.AtLeastOnePunctuationCharacterValidator',
    }, {
        'NAME': 'metashare.accounts.django_password_validators.AtLeastOneUppercaseCharacterValidator',
    }, {
        'NAME': 'metashare.accounts.django_password_validators.AtLeastOneLowercaseCharacterValidator',
     },{
        'NAME': 'metashare.accounts.django_password_validators.NoRepeatsValidator',
        'OPTIONS': {
            'max_repeats': 2,
        }
    }
]


@lru_cache.lru_cache(maxsize=None)
def get_default_password_validators():
    return get_password_validators(getattr(
        settings,
        'AUTH_PASSWORD_VALIDATORS',
        DEFAULT_AUTH_PASSWORD_VALIDATORS,
    ))


def get_password_validators(validator_config):
    validators = []
    for validator in validator_config:
        try:
            klass = import_string(validator['NAME'])
        except ImportError:
            msg = "The module in NAME could not be imported: %s. Check your AUTH_PASSWORD_VALIDATORS setting."
            raise ImproperlyConfigured(msg % validator['NAME'])
        validators.append(klass(**validator.get('OPTIONS', {})))

    return validators


def validate_password(password, user=None, password_validators=None):
    """
    Validate whether the password meets all validator requirements.
    If the password is valid, return ``None``.
    If the password is invalid, raise ValidationError with all error messages.
    """
    errors = []
    if password_validators is None:
        password_validators = get_default_password_validators()
    for validator in password_validators:
        try:
            validator.validate(password, user)
        except ValidationError as error:
            errors.append(error)
    if errors:
        raise ValidationError(errors)


def password_changed(password, user=None, password_validators=None):
    """
    Inform all validators that have implemented a password_changed() method
    that the password has been changed.
    """
    if password_validators is None:
        password_validators = get_default_password_validators()
    for validator in password_validators:
        password_changed = getattr(validator, 'password_changed', lambda *a: None)
        password_changed(password, user)


def password_validators_help_texts(password_validators=None):
    """
    Return a list of all help texts of all configured validators.
    """
    help_texts = []
    if password_validators is None:
        password_validators = get_default_password_validators()
    for validator in password_validators:
        help_texts.append(validator.get_help_text())
    return help_texts


def password_validators_help_text_html(password_validators=None):
    """
    Return an HTML string with all help texts of all configured validators
    in an <ul>.
    """
    help_texts = password_validators_help_texts(password_validators)
    if not help_texts:
        return ''
    help_items = format_html_join('', '<li>{}</li>', [(help_text,) for help_text in help_texts])
    return format_html('<ul>{}</ul>', help_items)


class MinimumLengthValidator(object):
    """
    Validate whether the password is of a minimum length.
    """
    def __init__(self, min_length=8):
        self.min_length = min_length

    def validate(self, password, user=None):
        if len(password) < self.min_length:
            raise ValidationError(
                ungettext_lazy(
                    "This password is too short. It must contain at least %(min_length)d character.",
                    "This password is too short. It must contain at least %(min_length)d characters.",
                    "min_length"
                ),
                code='password_too_short',
                params={'min_length': self.min_length},
            )

    def get_help_text(self):
        return ungettext_lazy(
            "Your password must contain at least %(min_length)d character.",
            "Your password must contain at least %(min_length)d characters.",
            "min_length"
        ) % {'min_length': self.min_length}


class UserAttributeSimilarityValidator(object):
    """
    Validate whether the password is sufficiently different from the user's
    attributes.
    If no specific attributes are provided, look at a sensible list of
    defaults. Attributes that don't exist are ignored. Comparison is made to
    not only the full attribute value, but also its components, so that, for
    example, a password is validated against either part of an email address,
    as well as the full address.
    """
    DEFAULT_USER_ATTRIBUTES = ('username') #, 'first_name', 'last_name', 'email')

    def __init__(self, user_attributes=DEFAULT_USER_ATTRIBUTES, max_similarity=0.5):
        self.user_attributes = user_attributes
        self.max_similarity = max_similarity

    def validate(self, password, user=None):
        if not user:
            return

        for attribute_name in self.user_attributes:
            value = getattr(user, attribute_name, None)
            if not value or not isinstance(value, string_types):
                continue
            value_parts = re.split('\W+', value) + [value]
            for value_part in value_parts:
                similarity = SequenceMatcher(a=password.lower(), b=value_part.lower()).quick_ratio()
                if similarity > self.max_similarity or similarity == 1 or self.max_similarity == 0:
                    verbose_name = force_text(user._meta.get_field(attribute_name).verbose_name)
                    raise ValidationError(
                        _("The password is too similar to the %(verbose_name)s."),
                        code='password_too_similar',
                        params={'verbose_name': verbose_name},
                    )

    def get_help_text(self):
        return _("Your password can't be too similar to your other personal information.")


class CommonPasswordValidator(object):
    """
    Validate whether the password is a common password.
    The password is rejected if it occurs in a provided list, which may be gzipped.
    The list Django ships with contains 1000 common passwords, created by Mark Burnett:
    https://xato.net/passwords/more-top-worst-passwords/
    """
    DEFAULT_PASSWORD_LIST_PATH = os.path.join(
        os.path.dirname(os.path.realpath(upath(__file__))), 'common-passwords.txt.gz'
    )

    def __init__(self, password_list_path=DEFAULT_PASSWORD_LIST_PATH):
        try:
            common_passwords_lines = gzip.open(password_list_path).read().decode('utf-8').splitlines()
        except IOError:
            with open(password_list_path) as f:
                common_passwords_lines = f.readlines()

        self.passwords = {p.strip() for p in common_passwords_lines}

    def validate(self, password, user=None):
        if password.lower().strip() in self.passwords:
            raise ValidationError(
                _("This password is too common."),
                code='password_too_common',
            )

    def get_help_text(self):
        return _("Your password can't be a commonly used password.")


class NumericPasswordValidator(object):
    """
    Validate whether the password is alphanumeric.
    """
    def validate(self, password, user=None):
        if password.isdigit():
            raise ValidationError(
                _("This password is entirely numeric."),
                code='password_entirely_numeric',
            )

    def get_help_text(self):
        return _("Your password can't be entirely numeric.")


class AtLeastOneDigitValidator(object):
    """
    Validate whether the password contains at least one digit.
    """
    def validate(self, password, user=None):
        if not any(c.isdigit() for c in password):
            raise ValidationError(
                _("This password does not contain a digit."),
                code='password_without_digit',
            )

    def get_help_text(self):
        return _("Your password must contain at least one digit.")


class AtLeastOnePunctuationCharacterValidator(object):
    """
    Validate whether the password contains at least one punctuation character.
    """
    def validate(self, password, user=None):
        if not any(c == ' ' or c in string.punctuation for c in password):
            raise ValidationError(
                _("This password does not contain a punctuation character."),
                code='password_without_punctuation',
            )

    def get_help_text(self):
        return _("Your password must contain at least one punctuation character.")


class AtLeastOneUppercaseCharacterValidator(object):
    """
    Validate whether the password contains at least one uppercase character.
    """
    def validate(self, password, user=None):
        if not any(c.isupper() for c in password):
            raise ValidationError(
                _("This password does not contain an uppercase character."),
                code='password_without_uppercase',
            )

    def get_help_text(self):
        return _("Your password must contain at least one uppercase character.")


class AtLeastOneLowercaseCharacterValidator(object):
    """
    Validate whether the password contains at least one lowercase character.
    """
    def validate(self, password, user=None):
        if not any(c.islower() for c in password):
            raise ValidationError(
                _("This password does not contain a lowercase character."),
                code='password_without_lowercase',
            )

    def get_help_text(self):
        return _("Your password must contain at least one lowercase character.")


class NoRepeatsValidator(object):
    """
    Validate whether the password does not contain a character repeated more than max_repeats times in a row.
    """
    def __init__(self, max_repeats=2):
        self.max_repeats = max_repeats

    def validate(self, password, user=None):
        chars = set(password)
        for c in chars:
            if c*(self.max_repeats+1) in password:
                raise ValidationError(
                    (_("This password contains a character repeated more than %d times in a row.") % self.max_repeats),
                    code='password_contains_repeats',
                )

    def get_help_text(self):
        return _("Your password can't contain a character repeated more than %d times in a row.") % self.max_repeats

    