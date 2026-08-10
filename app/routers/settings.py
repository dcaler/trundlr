from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.database import get_db
from app.email import send_test
from app.models import AppSettings
from app.schemas import SettingsRead, SettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _get_or_create(session: Session) -> AppSettings:
    s = session.get(AppSettings, 1)
    if not s:
        s = AppSettings()
        session.add(s)
        session.commit()
        session.refresh(s)
    return s


def _to_read(s: AppSettings) -> SettingsRead:
    return SettingsRead(
        timezone=s.timezone,
        caldav_default_project_id=s.caldav_default_project_id,
        notify_email=s.notify_email,
        smtp_host=s.smtp_host,
        smtp_port=s.smtp_port,
        smtp_user=s.smtp_user,
        smtp_from=s.smtp_from,
        smtp_tls=s.smtp_tls,
        smtp_password_set=bool(s.smtp_password),
    )


@router.get("/", response_model=SettingsRead)
def get_settings(session: Session = Depends(get_db)):
    return _to_read(_get_or_create(session))


@router.patch("/", response_model=SettingsRead)
def update_settings(data: SettingsUpdate, session: Session = Depends(get_db)):
    s = _get_or_create(session)
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(s, key, value)
    session.add(s)
    session.commit()
    session.refresh(s)
    return _to_read(s)


@router.post("/test-email")
def test_email(session: Session = Depends(get_db)):
    error = send_test(session)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"ok": True}
