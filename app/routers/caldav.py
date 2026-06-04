import hashlib
import os
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import unquote
from zoneinfo import ZoneInfo


from fastapi import APIRouter, Depends, Request, Response
from icalendar import Calendar, Event
from sqlmodel import Session, select

from app.database import get_db
from app.models import AppSettings, Project, Resource, Task, TaskResource

# ── Namespaces ─────────────────────────────────────────────────────────────

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CS = "http://calendarserver.org/ns/"

ET.register_namespace("d", DAV)
ET.register_namespace("cal", CALDAV)
ET.register_namespace("cs", CS)


def _d(t):
    return f"{{{DAV}}}{t}"


def _cal(t):
    return f"{{{CALDAV}}}{t}"


def _cs(t):
    return f"{{{CS}}}{t}"


# ── Router ─────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/caldav", tags=["caldav"])

_DAV_HEADERS = {
    "DAV": "1, 2, calendar-access",
    "Allow": "OPTIONS, GET, PUT, DELETE, PROPFIND, REPORT",
    "MS-Author-Via": "DAV",
}


# ── XML helpers ────────────────────────────────────────────────────────────

def _multistatus(responses: list, sync_token: Optional[str] = None) -> Response:
    """Build a 207 Multi-Status response.

    responses: list of (href, found, missing)
      found: dict[tag_str -> ET.Element | None]
        - None: render as self-closing element with no children/text
        - ET.Element with tag "_val": set .text on the prop element
        - ET.Element otherwise: append as child of the prop element
      missing: list[tag_str]
    sync_token: if set, appended as <d:sync-token> child of multistatus
                (required for sync-collection REPORT responses).
    """
    root = ET.Element(_d("multistatus"))

    for href_str, found, missing in responses:
        resp_el = ET.SubElement(root, _d("response"))
        ET.SubElement(resp_el, _d("href")).text = href_str

        if found:
            ps_ok = ET.SubElement(resp_el, _d("propstat"))
            props_el = ET.SubElement(ps_ok, _d("prop"))
            for tag, child in found.items():
                prop_el = ET.SubElement(props_el, tag)
                if child is None:
                    pass  # self-closing
                elif child.tag == "_val":
                    prop_el.text = child.text
                else:
                    for sub in list(child):
                        prop_el.append(sub)
            ET.SubElement(ps_ok, _d("status")).text = "HTTP/1.1 200 OK"

        if missing:
            ps_404 = ET.SubElement(resp_el, _d("propstat"))
            props_404 = ET.SubElement(ps_404, _d("prop"))
            for tag in missing:
                ET.SubElement(props_404, tag)
            ET.SubElement(ps_404, _d("status")).text = "HTTP/1.1 404 Not Found"

    if sync_token is not None:
        ET.SubElement(root, _d("sync-token")).text = f"urn:trundlr:sync:{sync_token}"

    xml_str = '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode")
    return Response(
        content=xml_str,
        status_code=207,
        media_type='application/xml; charset="utf-8"',
        headers=_DAV_HEADERS,
    )


def _val(text: str) -> ET.Element:
    """Sentinel: prop element gets this as its text content."""
    el = ET.Element("_val")
    el.text = text
    return el


def _children(*child_els: ET.Element) -> ET.Element:
    """Sentinel: prop element gets these as child elements."""
    wrapper = ET.Element("_children")
    for c in child_els:
        wrapper.append(c)
    return wrapper


def _href_child(url: str) -> ET.Element:
    return _children(_make_href(url))


def _make_href(url: str) -> ET.Element:
    el = ET.Element(_d("href"))
    el.text = url
    return el


def _requested_props(body: bytes) -> Optional[list]:
    if not body:
        return None
    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return None
    if root.find(_d("allprop")) is not None:
        return None
    prop_el = root.find(_d("prop"))
    if prop_el is None:
        return None
    return [child.tag for child in prop_el]


# ── ETag / CTag ────────────────────────────────────────────────────────────

def _task_etag(task: Task) -> str:
    status_val = task.status.value if hasattr(task.status, "value") else task.status
    raw = f"{task.id}:{task.title}:{task.description}:{task.start_date}:{task.end_date}:{status_val}"
    return f'"{hashlib.md5(raw.encode()).hexdigest()}"'


def _collection_ctag(tasks: list) -> str:
    # Return a unique value on every call so Apple Calendar always detects
    # a "changed" ctag between its two PROPFIND rounds and sends REPORT.
    # Full re-fetches are cheap at this scale.
    return os.urandom(8).hex()


# ── iCal helpers ───────────────────────────────────────────────────────────

def _task_to_ical(task: Task, resource: Resource, project_name: Optional[str], tz: ZoneInfo) -> str:
    cal = Calendar()
    cal.add("prodid", "-//Trundlr//CalDAV//EN")
    cal.add("version", "2.0")

    ev = Event()
    ev.add("uid", f"task-{task.id}@trundlr")

    summary = f"{project_name}: {task.title}" if project_name else task.title
    ev.add("summary", summary)
    if task.description:
        ev.add("description", task.description)

    def _to_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)

    if task.start_date:
        start_utc = _to_utc(task.start_date)
        ev.add("dtstart", start_utc)
        ev.add("dtend", _to_utc(task.end_date) if task.end_date else start_utc + timedelta(hours=1))
    else:
        today = date.today()
        ev.add("dtstart", today)
        ev.add("dtend", today + timedelta(days=1))

    # All tasks show as CONFIRMED so Apple Calendar displays them as normal
    # events. TENTATIVE renders as hatched and CANCELLED is silently hidden.
    ev.add("status", "CONFIRMED")

    ev.add("dtstamp", datetime.now(timezone.utc))
    cal.add_component(ev)
    return cal.to_ical().decode("utf-8")


def _parse_vevent(raw: bytes) -> Optional[dict]:
    try:
        cal = Calendar.from_ical(raw)
    except Exception:
        return None
    for component in cal.walk():
        if component.name == "VEVENT":
            uid = str(component.get("uid", ""))
            summary = str(component.get("summary", ""))
            description_raw = component.get("description")
            description = str(description_raw) if description_raw else None
            dtstart = component.get("dtstart")
            dtend = component.get("dtend")
            return {
                "uid": uid,
                "summary": summary,
                "description": description,
                "dtstart": dtstart.dt if dtstart else None,
                "dtend": dtend.dt if dtend else None,
            }
    return None


def _to_naive_dt(dt, tz: ZoneInfo) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime(dt.year, dt.month, dt.day, 0, 0, 0)
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.astimezone(tz).replace(tzinfo=None)
        return dt
    return None


def _parse_task_id(uid: str) -> Optional[int]:
    if uid.startswith("task-") and uid.endswith("@trundlr"):
        try:
            return int(uid[5:-8])
        except ValueError:
            return None
    return None


# ── DB helpers ─────────────────────────────────────────────────────────────

def _get_tz(session: Session) -> ZoneInfo:
    s = session.get(AppSettings, 1)
    return ZoneInfo(s.timezone if s else "UTC")


def _tasks_for_resource(session: Session, rid: int) -> list:
    task_ids = session.exec(
        select(TaskResource.task_id).where(TaskResource.resource_id == rid)
    ).all()
    if not task_ids:
        return []
    return list(session.exec(select(Task).where(Task.id.in_(task_ids))).all())


def _project_name_for_task(session: Session, task: Task) -> Optional[str]:
    project = session.get(Project, task.project_id)
    return project.name if project else None


# ── Prop builders ──────────────────────────────────────────────────────────

def _resourcetype_collection_calendar() -> ET.Element:
    coll = ET.Element(_d("collection"))
    cal_el = ET.Element(_cal("calendar"))
    return _children(coll, cal_el)


def _resourcetype_principal() -> ET.Element:
    return _children(ET.Element(_d("principal")))


def _resourcetype_collection() -> ET.Element:
    return _children(ET.Element(_d("collection")))


def _supported_calendar_component_set() -> ET.Element:
    comp = ET.Element(_cal("comp"))
    comp.set("name", "VEVENT")
    return _children(comp)


def _filter_props(all_props: dict, requested: Optional[list]) -> tuple:
    if requested is None:
        return dict(all_props), []
    found = {}
    missing = []
    for tag in requested:
        if tag in all_props:
            found[tag] = all_props[tag]
        else:
            missing.append(tag)
    return found, missing


def _calendar_collection_props(resource: Resource, ctag: str, requested: Optional[list]) -> tuple:
    all_props = {
        _d("resourcetype"): _resourcetype_collection_calendar(),
        _d("displayname"): _val(resource.name),
        _cs("getctag"): _val(ctag),
        _d("sync-token"): _val(f"urn:trundlr:sync:{ctag}"),
        _cal("supported-calendar-component-set"): _supported_calendar_component_set(),
    }
    return _filter_props(all_props, requested)


# ── OPTIONS ────────────────────────────────────────────────────────────────

@router.api_route("/{path:path}", methods=["OPTIONS"])
def caldav_options(path: str):
    return Response(status_code=200, headers=_DAV_HEADERS)


# ── PROPFIND /caldav/ ─────────────────────────────────────────────────────
# Must return 207, not a redirect — CalDAV clients don't reliably follow
# PROPFIND redirects, causing "unsupported location" errors in Apple Calendar.

@router.api_route("/", methods=["PROPFIND"])
async def caldav_root_propfind(request: Request):
    body = await request.body()
    requested = _requested_props(body)
    all_props = {
        _d("resourcetype"):           _resourcetype_collection(),
        _d("current-user-principal"): _href_child("/caldav/principal/"),
        _cal("calendar-home-set"):    _href_child("/caldav/calendars/"),
    }
    found, missing = _filter_props(all_props, requested)
    return _multistatus([("/caldav/", found, missing)])


# ── PROPFIND /caldav/principal/ ────────────────────────────────────────────

@router.api_route("/principal/", methods=["PROPFIND"])
async def caldav_principal(request: Request):
    body = await request.body()
    requested = _requested_props(body)

    all_props = {
        _d("resourcetype"): _resourcetype_principal(),
        _d("displayname"): _val("Trundlr"),
        _d("current-user-principal"): _href_child("/caldav/principal/"),
        _d("principal-URL"): _href_child("/caldav/principal/"),
        _cal("calendar-home-set"): _href_child("/caldav/calendars/"),
    }
    found, missing = _filter_props(all_props, requested)
    return _multistatus([("/caldav/principal/", found, missing)])


# ── PROPFIND /caldav/calendars/ ────────────────────────────────────────────

@router.api_route("/calendars/", methods=["PROPFIND"])
async def caldav_calendars_home(request: Request, session: Session = Depends(get_db)):
    body = await request.body()
    requested = _requested_props(body)
    depth = request.headers.get("Depth", "0")

    home_all_props = {
        _d("resourcetype"): _resourcetype_collection(),
        _d("displayname"): _val("Calendars"),
        _d("current-user-principal"): _href_child("/caldav/principal/"),
        _cal("calendar-home-set"): _href_child("/caldav/calendars/"),
    }
    home_found, home_missing = _filter_props(home_all_props, requested)
    responses = [("/caldav/calendars/", home_found, home_missing)]

    if depth == "1":
        resources = session.exec(select(Resource)).all()
        for resource in resources:
            tasks = _tasks_for_resource(session, resource.id)
            ctag = _collection_ctag(tasks)
            found, missing = _calendar_collection_props(resource, ctag, requested)
            responses.append((f"/caldav/calendars/{resource.id}/", found, missing))

    return _multistatus(responses)


# ── PROPPATCH /caldav/calendars/{rid}/ ─────────────────────────────────────
# Apple Calendar uses PROPPATCH to set calendar colour, display name, etc.
# We don't persist these, but must return 207 or clients error out.

@router.api_route("/calendars/{rid}/", methods=["PROPPATCH"])
async def caldav_calendar_proppatch(rid: int, request: Request, session: Session = Depends(get_db)):
    if not session.get(Resource, rid):
        return Response(status_code=404)
    body = await request.body()
    props = []
    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
        for action in root:           # d:set / d:remove
            prop_el = action.find(_d("prop"))
            if prop_el is not None:
                props.extend(child.tag for child in prop_el)
    except ET.ParseError:
        pass
    return _multistatus([(f"/caldav/calendars/{rid}/", {tag: None for tag in props}, [])])


# ── PROPFIND /caldav/calendars/{rid}/ ──────────────────────────────────────

def _event_member_props(task: Task, requested: Optional[list]) -> tuple:
    all_props = {
        _d("resourcetype"): None,  # self-closing: a plain resource, not a collection
        _d("getetag"): _val(_task_etag(task)),
        _d("getcontenttype"): _val("text/calendar; component=vevent"),
    }
    return _filter_props(all_props, requested)


@router.api_route("/calendars/{rid}/", methods=["PROPFIND"])
async def caldav_calendar_propfind(rid: int, request: Request, session: Session = Depends(get_db)):
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)
    body = await request.body()
    requested = _requested_props(body)
    depth = request.headers.get("Depth", "0")
    print(f"[caldav] PROPFIND /calendars/{rid}/ Depth={depth} body={body.decode('utf-8', 'replace')!r}", flush=True)

    tasks = _tasks_for_resource(session, rid)
    ctag = _collection_ctag(tasks)
    found, missing = _calendar_collection_props(resource, ctag, requested)
    responses = [(f"/caldav/calendars/{rid}/", found, missing)]

    # Depth:1 enumerates the calendar's event members — the classic WebDAV
    # listing path Apple Calendar uses to discover events.
    if depth == "1":
        for task in tasks:
            m_found, m_missing = _event_member_props(task, requested)
            href = f"/caldav/calendars/{rid}/task-{task.id}@trundlr.ics"
            responses.append((href, m_found, m_missing))

    return _multistatus(responses)


# ── REPORT /caldav/calendars/{rid}/ ────────────────────────────────────────

@router.api_route("/calendars/{rid}/", methods=["REPORT"])
async def caldav_calendar_report(rid: int, request: Request, session: Session = Depends(get_db)):
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)

    body = await request.body()
    tz = _get_tz(session)
    print(f"[caldav] REPORT /calendars/{rid}/ body={body.decode('utf-8', 'replace')!r}", flush=True)

    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return Response(status_code=400, content="Bad XML")

    tasks = _tasks_for_resource(session, rid)
    ctag = _collection_ctag(tasks)
    is_sync_collection = root.tag == _d("sync-collection")

    if root.tag == _cal("calendar-multiget"):
        hrefs = [el.text for el in root.findall(_d("href")) if el.text]
        wanted_ids = set()
        for href in hrefs:
            # Hrefs arrive percent-encoded (e.g. task-28%40trundlr.ics) — decode
            # before parsing or the "@trundlr" suffix check never matches.
            filename = unquote(href).rstrip("/").rsplit("/", 1)[-1]
            uid = filename[:-4] if filename.endswith(".ics") else filename
            task_id = _parse_task_id(uid)
            if task_id is not None:
                wanted_ids.add(task_id)
        tasks = [t for t in tasks if t.id in wanted_ids]

    responses = []
    for task in tasks:
        project_name = _project_name_for_task(session, task)
        ical_str = _task_to_ical(task, resource, project_name, tz)
        etag = _task_etag(task)
        href = f"/caldav/calendars/{rid}/task-{task.id}@trundlr.ics"

        etag_el = ET.Element("_val")
        etag_el.text = etag
        caldata_el = ET.Element("_val")
        caldata_el.text = ical_str

        responses.append((href, {
            _d("getetag"): etag_el,
            _cal("calendar-data"): caldata_el,
        }, []))

    # sync-collection responses must include <d:sync-token>; include it for
    # all REPORT types so Apple Calendar establishes a sync baseline.
    return _multistatus(responses, sync_token=ctag)


# ── GET /caldav/calendars/{rid}/{uid_file} ─────────────────────────────────

@router.get("/calendars/{rid}/{uid_file}")
def caldav_get_event(rid: int, uid_file: str, session: Session = Depends(get_db)):
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)

    uid = uid_file[:-4] if uid_file.endswith(".ics") else uid_file
    task_id = _parse_task_id(uid)
    if task_id is None:
        return Response(status_code=404)

    task = session.get(Task, task_id)
    if not task:
        return Response(status_code=404)

    tz = _get_tz(session)
    project_name = _project_name_for_task(session, task)
    ical_str = _task_to_ical(task, resource, project_name, tz)
    etag = _task_etag(task)

    return Response(
        content=ical_str,
        media_type="text/calendar; charset=utf-8",
        headers={"ETag": etag, **_DAV_HEADERS},
    )


# ── PUT /caldav/calendars/{rid}/{uid_file} ─────────────────────────────────

@router.put("/calendars/{rid}/{uid_file}")
async def caldav_put_event(rid: int, uid_file: str, request: Request, session: Session = Depends(get_db)):
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)

    body = await request.body()
    parsed = _parse_vevent(body)
    if parsed is None:
        return Response(status_code=400, content="Cannot parse iCal body")
    if not parsed.get("uid"):
        return Response(status_code=400, content="No UID in iCal body")

    tz = _get_tz(session)
    uid = parsed["uid"]
    task_id = _parse_task_id(uid)

    if task_id is not None:
        task = session.get(Task, task_id)
        if task:
            summary = parsed["summary"] or ""
            project_name = _project_name_for_task(session, task)
            if project_name and summary.startswith(f"{project_name}: "):
                summary = summary[len(project_name) + 2:]
            if summary:
                task.title = summary
            task.description = parsed["description"]
            task.start_date = _to_naive_dt(parsed["dtstart"], tz)
            task.end_date = _to_naive_dt(parsed["dtend"], tz)
            session.add(task)
            session.commit()
            session.refresh(task)
            etag = _task_etag(task)
            return Response(status_code=204, headers={"ETag": etag, **_DAV_HEADERS})

    # New task from client — UID not recognised or not in our format
    app_settings = session.get(AppSettings, 1)
    project_id = app_settings.caldav_default_project_id if app_settings else None
    if project_id is None:
        first_project = session.exec(select(Project)).first()
        if not first_project:
            return Response(status_code=422, content="No projects exist; create one first")
        project_id = first_project.id

    summary = parsed["summary"] or "Untitled"
    project = session.get(Project, project_id)
    project_name = project.name if project else None
    if project_name and summary.startswith(f"{project_name}: "):
        summary = summary[len(project_name) + 2:]

    task = Task(
        title=summary,
        description=parsed["description"],
        project_id=project_id,
        start_date=_to_naive_dt(parsed["dtstart"], tz),
        end_date=_to_naive_dt(parsed["dtend"], tz),
    )
    session.add(task)
    session.flush()
    session.add(TaskResource(task_id=task.id, resource_id=rid))
    session.commit()
    session.refresh(task)

    location = f"/caldav/calendars/{rid}/task-{task.id}@trundlr.ics"
    etag = _task_etag(task)
    return Response(status_code=201, headers={"Location": location, "ETag": etag, **_DAV_HEADERS})


# ── DELETE /caldav/calendars/{rid}/{uid_file} ──────────────────────────────

@router.delete("/calendars/{rid}/{uid_file}")
def caldav_delete_event(rid: int, uid_file: str, session: Session = Depends(get_db)):
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)

    uid = uid_file[:-4] if uid_file.endswith(".ics") else uid_file
    task_id = _parse_task_id(uid)
    if task_id is None:
        return Response(status_code=404)

    task = session.get(Task, task_id)
    if not task:
        return Response(status_code=404)

    for tr in session.exec(select(TaskResource).where(TaskResource.task_id == task_id)).all():
        session.delete(tr)
    session.delete(task)
    session.commit()
    return Response(status_code=204, headers=_DAV_HEADERS)
