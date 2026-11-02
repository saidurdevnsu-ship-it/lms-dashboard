import logging
from urllib.parse import urlsplit

from django.conf import settings
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.template.loader import render_to_string

from notifications.mail import send_email

logger = logging.getLogger(__name__)


class SignInForm(AuthenticationForm):
    """Signs in only accounts that can use the dashboard, so others get a clear message instead of a sign-in loop."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise forms.ValidationError(
                "This account doesn't have access to STS LMS yet. Ask the school admin to give it access.",
                code="no_access",
            )


class ResetPasswordForm(PasswordResetForm):
    """"Forgot password" form whose email goes out like the welcome email: through Make when it's set up,
    with links on SITE_URL."""

    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email,
                  html_email_template_name=None):
        if settings.SITE_URL:
            parts = urlsplit(settings.SITE_URL)
            context = {**context, "protocol": parts.scheme, "domain": parts.netloc}
        subject = "".join(render_to_string(subject_template_name, context).splitlines())
        text = render_to_string(email_template_name, context)
        html = render_to_string(html_email_template_name, context) if html_email_template_name else ""
        try:
            send_email("user.password_reset", to_email, subject, text, html)
        except Exception:
            logger.exception("Couldn't send the password reset email to %s", to_email)


class ProfileForm(forms.ModelForm):
    """"My profile": the signed-in user's own name and email. (Employees' phone and address are hr's
    EmployeeContactForm, shown on the same page.)

    Changing the email needs the current password: password resets go to that address, so a session left open
    mustn't be enough to take the account over."""

    current_password = forms.CharField(
        required=False, strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text="Needed only when you change your email.")

    class Meta:
        model = get_user_model()
        fields = ("first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_email = self.instance.email
        self.fields["email"].required = True

    def email_changed(self):
        email = self.cleaned_data.get("email")
        return bool(email) and email.lower() != self.original_email.lower()

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        # Only a new address must be free: older accounts may already share one, and that mustn't stop them
        # from saving anything else.
        if email.lower() != self.original_email.lower():
            if get_user_model().objects.exclude(pk=self.instance.pk).filter(email__iexact=email).exists():
                raise forms.ValidationError("Another account already uses this email.")
        return email

    def clean(self):
        cleaned = super().clean()
        if self.email_changed() and not self.instance.check_password(cleaned.get("current_password") or ""):
            # Someone may be trying an open session; leave a trace for whoever looks into it.
            logger.warning("Wrong current password when %s tried to change their email", self.instance.get_username())
            self.add_error("current_password", "Enter your current password to change your email.")
        return cleaned
