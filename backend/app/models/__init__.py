"""ORM models.

Every model inherits from `app.db.base.Base`, so one metadata object describes the whole schema.
The schema itself is owned by the migrations in `alembic/versions`, not by these classes: the
load-bearing pieces (the partial unique index, the GiST exclusion constraint, the privilege
revocation on `change_logs`) cannot be expressed as model attributes, so a model is a mapping onto
an existing table rather than the definition of it.
"""

from __future__ import annotations

from app.models.billing import BillingRecord
from app.models.change_log import ChangeLog
from app.models.client import Client
from app.models.document import Document, DocumentType
from app.models.employee import Employee, EmployeeRate, EmployeeStatus
from app.models.export import Export, ExportFormat, ExportReportType, ExportStatus
from app.models.notification import Notification, NotificationSeverity
from app.models.payroll import (
    CalculationStatus,
    PayrollRecord,
    PayrollSiteAllocation,
)
from app.models.period_lock import PeriodLock
from app.models.setting import Setting, SettingValueType
from app.models.site import (
    AssignmentMode,
    EmployeeSite,
    QrMode,
    Site,
    SiteRate,
    SiteStatus,
)
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import AppLanguage, User, UserRole
from app.models.user_site import UserSite

__all__ = [
    "AppLanguage",
    "AssignmentMode",
    "BillingRecord",
    "CalculationStatus",
    "ChangeLog",
    "Client",
    "Document",
    "DocumentType",
    "Employee",
    "EmployeeRate",
    "EmployeeSite",
    "EmployeeStatus",
    "Export",
    "ExportFormat",
    "ExportReportType",
    "ExportStatus",
    "Notification",
    "NotificationSeverity",
    "PayrollRecord",
    "PayrollSiteAllocation",
    "PeriodLock",
    "QrMode",
    "Setting",
    "SettingValueType",
    "Site",
    "SiteRate",
    "SiteStatus",
    "TimeEntry",
    "TimeEntrySource",
    "TimeEntryStatus",
    "User",
    "UserRole",
    "UserSite",
]
