import hashlib
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Optional
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

def _multistatus(responses: list) -> Response:
    """Build a 207 Multi-Status response.

    responses: list of (href, found, missing)
      found: dict[tag_str -> ET.Element | None]
        - None: render as self-closing element with no children/text
        - ET.Element with tag "_val": set .text on the prop element
        - ET.Element otherwise: append as child of the prop element
      missing: list[tag_str]
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
    parts = sorted(
        f"{t.id},{t.status.value if hasattr(t.status, 'value') else t.status},{t.start_date}"
        for t in tasks
    )
    return hashlib.md5("\n".join(parts).encode()).hexdigest()


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

    if task.start_date:
        start_dt = task.start_date
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=tz)
        else:
            start_dt = start_dt.astimezone(tz)
        ev.add("dtstart", start_dt)

        if task.end_date:
            end_dt = task.end_date
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=tz)
            else:
                end_dt = end_dt.astimezone(tz)
        else:
            end_dt = start_dt + timedelta(hours=1)
        ev.add("dtend", end_dt)
    else:
        today = date.today()
        ev.add("dtstart", today)
        ev.add("dtend", today + timedelta(days=1))

    status_val = task.status.value if hasattr(task.status, "value") else task.status
    if status_val in ("todo", "blocked"):
        ev.add("status", "TENTATIVE")
    elif status_val == "in_progress":
        ev.add("status", "CONFIRMED")
    else:
        ev.add("status", "CANCELLED")

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
        _cal("supported-calendar-component-set"): _supported_calendar_component_set(),
    }
    return _filter_props(all_props, requested)


# ── OPTIONS ────────────────────────────────────────────────────────────────

@router.api_route("/{path:path}", methods=["OPTIONS"])
def caldav_options(path: str):
    return Response(status_code=200, headers=_DAV_HEADERS)


# ── PROPFIND /caldav/ → redirect ───────────────────────────────────────────

@router.api_route("/", methods=["PROPFIND"])
def caldav_root_propfind():
    return Response(
        status_code=301,
        headers={"Location": "/caldav/principal/", **_DAV_HEADERS},
    )


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


# ── PROPFIND /caldav/calendars/{rid}/ ──────────────────────────────────────

@router.api_route("/calendars/{rid}/", methods=["PROPFIND"])
async def caldav_calendar_propfind(rid: int, request: Request, session: Session = Depends(get_db)):
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)
    body = await request.body()
    requested = _requested_props(body)
    tasks = _tasks_for_resource(session, rid)
    ctag = _collection_ctag(tasks)
    found, missing = _calendar_collection_props(resource, ctag, requested)
    return _multistatus([(f"/caldav/calendars/{rid}/", found, missing)])


# ── REPORT /caldav/calendars/{rid}/ ────────────────────────────────────────

@router.api_route("/calendars/{rid}/", methods=["REPORT"])
async def caldav_calendar_report(rid: int, request: Request, session: Session = Depends(get_db)):
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)

    body = await request.body()
    tz = _get_tz(session)

    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return Response(status_code=400, content="Bad XML")

    tasks = _tasks_for_resource(session, rid)

    if root.tag == _cal("calendar-multiget"):
        hrefs = [el.text for el in root.findall(_d("href")) if el.text]
        wanted_ids = set()
        for href in hrefs:
            filename = href.rstrip("/").rsplit("/", 1)[-1]
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

    return _multistatus(responses)


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
