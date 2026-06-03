from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_db
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


@router.get("/", response_model=SettingsRead)
def get_settings(session: Session = Depends(get_db)):
    return _get_or_create(session)


@router.patch("/", response_model=SettingsRead)
def update_settings(data: SettingsUpdate, session: Session = Depends(get_db)):
    s = _get_or_create(session)
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(s, key, value)
    session.add(s)
    session.commit()
    session.refresh(s)
    return s
