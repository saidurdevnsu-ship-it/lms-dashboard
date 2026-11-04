"""Who counts as an HR manager. Kept apart from hr.admin so hr.services can use it without importing the admin
(which imports the services)."""


def is_hr_manager(user):
    return user.is_superuser or user.has_perm("hr.manage_hr")
