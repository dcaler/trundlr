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
from app.models import AppSettings, Project, Resource, ResourceCalBlock, Task, TaskResource

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

def _multistatus(responses: list, sync_token: Optional[str] = None,
                 gone: Optional[list] = None) -> Response:
    """Build a 207 Multi-Status response.

    responses: list of (href, found, missing)
      found: dict[tag_str -> ET.Element | None]
        - None: render as self-closing element with no children/text
        - ET.Element with tag "_val": set .text on the prop element
        - ET.Element otherwise: append as child of the prop element
      missing: list[tag_str]
    sync_token: if set, appended as <d:sync-token> child of multistatus
                (required for sync-collection REPORT responses).
    gone: list of hrefs to render as <response><href/><status>404</status>,
          i.e. members that were removed since the client's last sync. This is
          how a sync-collection REPORT tells the client to delete an event.
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

    for href_str in (gone or []):
        resp_el = ET.SubElement(root, _d("response"))
        ET.SubElement(resp_el, _d("href")).text = href_str
        ET.SubElement(resp_el, _d("status")).text = "HTTP/1.1 404 Not Found"

    if sync_token is not None:
        ET.SubElement(root, _d("sync-token")).text = sync_token

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


# ── ETag / CTag / sync-token ────────────────────────────────────────────────

def _task_etag(task: Task, project_name: Optional[str] = None) -> str:
    # project_name is part of the event summary ("{project}: {title}"), so it
    # must feed the etag too — otherwise renaming a project leaves Apple with a
    # stale title (unchanged etag → no re-fetch). Callers that have the project
    # name MUST pass it, consistently, so the listing etag matches the data etag.
    status_val = task.status.value if hasattr(task.status, "value") else task.status
    raw = f"{task.id}:{project_name}:{task.title}:{task.description}:{task.start_date}:{task.end_date}:{status_val}"
    return f'"{hashlib.md5(raw.encode()).hexdigest()}"'


def _block_etag(block: ResourceCalBlock) -> str:
    raw = f"block:{block.id}:{block.summary}:{block.start}:{block.end}"
    return f'"{hashlib.md5(raw.encode()).hexdigest()}"'


def _collection_ctag(tasks: list) -> str:
    # Return a unique value on every call so Apple Calendar always detects
    # a "changed" ctag between its two PROPFIND rounds and sends REPORT.
    # Full re-fetches are cheap at this scale.
    return os.urandom(8).hex()


def _scheduled(tasks: list) -> list:
    # Only tasks with a start_date are real calendar events. Unscheduled tasks
    # have no time, so emitting them produces bogus all-day events — skip them
    # everywhere (matches the legacy /api/resources/{id}/calendar.ics feed).
    return [t for t in tasks if t.start_date is not None]


_SYNC_PREFIX = "urn:trundlr:sync:"


def _sync_token(keys: set) -> str:
    # The token encodes the set of member keys present at this sync, plus a
    # nonce so it changes every time (Apple then always issues a fresh
    # sync-collection REPORT). On the next REPORT the client echoes this token
    # back; we decode the key set and diff it against current keys to discover
    # which events were deleted — no server-side tombstones required. Keys are
    # opaque strings (task ids for tasks, resource names for blocks).
    payload = ",".join(sorted(str(k) for k in keys))
    return f"{_SYNC_PREFIX}{payload}|{os.urandom(4).hex()}"


def _parse_sync_token_keys(token: Optional[str]) -> set:
    if not token or not token.startswith(_SYNC_PREFIX):
        return set()
    key_part = token[len(_SYNC_PREFIX):].split("|", 1)[0]
    return {c for c in key_part.split(",") if c}


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

    # Callers only pass scheduled tasks (start_date set); _scheduled() filters
    # the unscheduled ones out upstream. A timed DTSTART/DTEND keeps the event
    # from rendering as bogus all-day in Apple Calendar.
    start_utc = _to_utc(task.start_date)
    ev.add("dtstart", start_utc)
    ev.add("dtend", _to_utc(task.end_date) if task.end_date else start_utc + timedelta(hours=1))

    # All tasks show as CONFIRMED so Apple Calendar displays them as normal
    # events. TENTATIVE renders as hatched and CANCELLED is silently hidden.
    ev.add("status", "CONFIRMED")

    ev.add("dtstamp", datetime.now(timezone.utc))
    cal.add_component(ev)
    return cal.to_ical().decode("utf-8")


def _block_to_ical(block: ResourceCalBlock, tz: ZoneInfo) -> str:
    cal = Calendar()
    cal.add("prodid", "-//Trundlr//CalDAV//EN")
    cal.add("version", "2.0")

    ev = Event()
    ev.add("uid", block.uid)
    ev.add("summary", block.summary or "Blocked")

    def _to_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)

    start_utc = _to_utc(block.start)
    ev.add("dtstart", start_utc)
    ev.add("dtend", _to_utc(block.end) if block.end else start_utc + timedelta(hours=1))
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


def _resolve_cal(cal: str) -> Optional[tuple]:
    """Map a calendar path segment to (resource_id, is_block).

    "5" → (5, False) the task calendar; "block-5" → (5, True) the block calendar.
    Returns None if the id isn't an integer.
    """
    is_block = cal.startswith("block-")
    raw = cal[len("block-"):] if is_block else cal
    try:
        return int(raw), is_block
    except ValueError:
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


def _blocks_for_resource(session: Session, rid: int) -> list:
    return list(session.exec(
        select(ResourceCalBlock).where(ResourceCalBlock.resource_id == rid)
    ).all())


def _block_by_uid(session: Session, rid: int, uid: str):
    return session.exec(
        select(ResourceCalBlock).where(
            ResourceCalBlock.resource_id == rid, ResourceCalBlock.uid == uid
        )
    ).first()


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


def _calendar_collection_props(display_name: str, ctag: str, sync_token: str,
                               requested: Optional[list]) -> tuple:
    all_props = {
        _d("resourcetype"): _resourcetype_collection_calendar(),
        _d("displayname"): _val(display_name),
        _cs("getctag"): _val(ctag),
        _d("sync-token"): _val(sync_token),
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
            # Task calendar.
            tasks = _scheduled(_tasks_for_resource(session, resource.id))
            t_found, t_missing = _calendar_collection_props(
                resource.name, _collection_ctag(tasks),
                _sync_token({str(t.id) for t in tasks}), requested,
            )
            responses.append((f"/caldav/calendars/{resource.id}/", t_found, t_missing))

            # Block calendar — paint events here to block scheduling.
            blocks = _blocks_for_resource(session, resource.id)
            b_found, b_missing = _calendar_collection_props(
                f"{resource.name}-block", _collection_ctag(blocks),
                _sync_token({b.uid for b in blocks}), requested,
            )
            responses.append((f"/caldav/calendars/block-{resource.id}/", b_found, b_missing))

    return _multistatus(responses)


# ── PROPPATCH /caldav/calendars/{cal}/ ─────────────────────────────────────
# Apple Calendar uses PROPPATCH to set calendar colour, display name, etc.
# We don't persist these, but must return 207 or clients error out.

@router.api_route("/calendars/{cal}/", methods=["PROPPATCH"])
async def caldav_calendar_proppatch(cal: str, request: Request, session: Session = Depends(get_db)):
    resolved = _resolve_cal(cal)
    if resolved is None or not session.get(Resource, resolved[0]):
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
    return _multistatus([(f"/caldav/calendars/{cal}/", {tag: None for tag in props}, [])])


# ── PROPFIND /caldav/calendars/{cal}/ ──────────────────────────────────────

def _member_key(member, is_block: bool) -> str:
    """Opaque sync/identity key: a block's CalDAV resource name, else the task id."""
    return member.uid if is_block else str(member.id)


def _member_href(cal: str, key: str, is_block: bool) -> str:
    # Blocks live at the client-chosen resource name; tasks at task-{id}@trundlr.
    name = key if is_block else f"task-{key}@trundlr"
    return f"/caldav/calendars/{cal}/{name}.ics"


def _key_from_filename(name: str, is_block: bool) -> Optional[str]:
    """Resolve a member key from an href filename (already stripped of .ics)."""
    if is_block:
        return name or None
    tid = _parse_task_id(name)
    return str(tid) if tid is not None else None


def _member_props(etag: str, requested: Optional[list]) -> tuple:
    all_props = {
        _d("resourcetype"): None,  # self-closing: a plain resource, not a collection
        _d("getetag"): _val(etag),
        _d("getcontenttype"): _val("text/calendar; component=vevent"),
    }
    return _filter_props(all_props, requested)


@router.api_route("/calendars/{cal}/", methods=["PROPFIND"])
async def caldav_calendar_propfind(cal: str, request: Request, session: Session = Depends(get_db)):
    resolved = _resolve_cal(cal)
    if resolved is None:
        return Response(status_code=404)
    rid, is_block = resolved
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)
    body = await request.body()
    requested = _requested_props(body)
    depth = request.headers.get("Depth", "0")

    if is_block:
        members = _blocks_for_resource(session, rid)
        display = f"{resource.name}-block"
    else:
        members = _scheduled(_tasks_for_resource(session, rid))
        display = resource.name

    ctag = _collection_ctag(members)
    sync_token = _sync_token({_member_key(m, is_block) for m in members})
    found, missing = _calendar_collection_props(display, ctag, sync_token, requested)
    responses = [(f"/caldav/calendars/{cal}/", found, missing)]

    # Depth:1 enumerates the calendar's event members — the classic WebDAV
    # listing path Apple Calendar uses to discover events.
    if depth == "1":
        for m in members:
            if is_block:
                etag = _block_etag(m)
            else:
                etag = _task_etag(m, _project_name_for_task(session, m))
            href = _member_href(cal, _member_key(m, is_block), is_block)
            m_found, m_missing = _member_props(etag, requested)
            responses.append((href, m_found, m_missing))

    return _multistatus(responses)


# ── REPORT /caldav/calendars/{cal}/ ────────────────────────────────────────

@router.api_route("/calendars/{cal}/", methods=["REPORT"])
async def caldav_calendar_report(cal: str, request: Request, session: Session = Depends(get_db)):
    resolved = _resolve_cal(cal)
    if resolved is None:
        return Response(status_code=404)
    rid, is_block = resolved
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)

    body = await request.body()
    tz = _get_tz(session)

    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return Response(status_code=400, content="Bad XML")

    # Members are blocks (block calendar) or scheduled tasks (task calendar).
    # An unscheduled/deleted member that the client still knows about must be
    # reported as removed (404) so it isn't orphaned.
    if is_block:
        members = _blocks_for_resource(session, rid)
    else:
        members = _scheduled(_tasks_for_resource(session, rid))
    by_key = {_member_key(m, is_block): m for m in members}
    current_keys = set(by_key)

    def _member_response(member):
        if is_block:
            ical_str = _block_to_ical(member, tz)
            etag = _block_etag(member)
        else:
            project_name = _project_name_for_task(session, member)
            ical_str = _task_to_ical(member, resource, project_name, tz)
            etag = _task_etag(member, project_name)
        etag_el = ET.Element("_val")
        etag_el.text = etag
        caldata_el = ET.Element("_val")
        caldata_el.text = ical_str
        href = _member_href(cal, _member_key(member, is_block), is_block)
        return (href, {
            _d("getetag"): etag_el,
            _cal("calendar-data"): caldata_el,
        }, [])

    responses = []
    gone = []

    if root.tag == _cal("calendar-multiget"):
        # Client asks for specific hrefs. Return data for the ones that still
        # exist; 404 the rest so a deleted/unscheduled event is dropped.
        for el in root.findall(_d("href")):
            if not el.text:
                continue
            # Hrefs arrive percent-encoded (e.g. task-28%40trundlr.ics) — decode
            # before parsing or the "@trundlr" suffix check never matches.
            filename = unquote(el.text).rstrip("/").rsplit("/", 1)[-1]
            name = filename[:-4] if filename.endswith(".ics") else filename
            key = _key_from_filename(name, is_block)
            if key is not None and key in current_keys:
                responses.append(_member_response(by_key[key]))
            else:
                gone.append(el.text)
        # multiget is href-driven, not delta-driven: no sync-token needed.
        return _multistatus(responses, gone=gone)

    # sync-collection (and any other REPORT): return all current events, and
    # 404 every event the client knew about last time that is now gone. The
    # prior key set is decoded from the sync-token the client echoes back.
    prior_keys = _parse_sync_token_keys(root.findtext(_d("sync-token")))
    for member in members:
        responses.append(_member_response(member))
    for removed_key in sorted(prior_keys - current_keys):
        gone.append(_member_href(cal, removed_key, is_block))

    return _multistatus(responses, gone=gone, sync_token=_sync_token(current_keys))


# ── GET /caldav/calendars/{cal}/{uid_file} ─────────────────────────────────

@router.get("/calendars/{cal}/{uid_file}")
def caldav_get_event(cal: str, uid_file: str, session: Session = Depends(get_db)):
    resolved = _resolve_cal(cal)
    if resolved is None:
        return Response(status_code=404)
    rid, is_block = resolved
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)

    uid = uid_file[:-4] if uid_file.endswith(".ics") else uid_file
    tz = _get_tz(session)

    if is_block:
        block = _block_by_uid(session, rid, uid)
        if not block:
            return Response(status_code=404)
        return Response(
            content=_block_to_ical(block, tz),
            media_type="text/calendar; charset=utf-8",
            headers={"ETag": _block_etag(block), **_DAV_HEADERS},
        )

    task_id = _parse_task_id(uid)
    if task_id is None:
        return Response(status_code=404)
    task = session.get(Task, task_id)
    if not task or task.start_date is None:
        # Unscheduled tasks are not calendar events (see _scheduled).
        return Response(status_code=404)

    project_name = _project_name_for_task(session, task)
    ical_str = _task_to_ical(task, resource, project_name, tz)
    etag = _task_etag(task, project_name)

    return Response(
        content=ical_str,
        media_type="text/calendar; charset=utf-8",
        headers={"ETag": etag, **_DAV_HEADERS},
    )


# ── PUT /caldav/calendars/{cal}/{uid_file} ─────────────────────────────────

def _put_block(session: Session, rid: int, cal: str, name: str, parsed: dict, tz: ZoneInfo) -> Response:
    """Create or update a ResourceCalBlock from a PUT to the block calendar.

    `name` is the CalDAV resource name (href filename, sans .ics) the client
    chose. We store and serve the block under exactly that name so the client
    never sees a server-renamed copy — which is what caused duplicate events.
    """
    start = _to_naive_dt(parsed["dtstart"], tz)
    if start is None:
        return Response(status_code=400, content="Block event needs a start time")
    end = _to_naive_dt(parsed["dtend"], tz) or (start + timedelta(hours=1))
    summary = parsed["summary"] or None

    block = _block_by_uid(session, rid, name)
    created = block is None
    if created:
        block = ResourceCalBlock(resource_id=rid, uid=name)
    block.start = start
    block.end = end
    block.summary = summary
    session.add(block)
    session.commit()
    session.refresh(block)

    headers = {"ETag": _block_etag(block), **_DAV_HEADERS}
    # 201 with no Location: the resource lives at the same URL the client PUT to.
    return Response(status_code=201 if created else 204, headers=headers)


@router.put("/calendars/{cal}/{uid_file}")
async def caldav_put_event(cal: str, uid_file: str, request: Request, session: Session = Depends(get_db)):
    resolved = _resolve_cal(cal)
    if resolved is None:
        return Response(status_code=404)
    rid, is_block = resolved
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

    if is_block:
        name = uid_file[:-4] if uid_file.endswith(".ics") else uid_file
        return _put_block(session, rid, cal, name, parsed, tz)

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
            etag = _task_etag(task, project_name)
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
    etag = _task_etag(task, project_name)
    return Response(status_code=201, headers={"Location": location, "ETag": etag, **_DAV_HEADERS})


# ── DELETE /caldav/calendars/{cal}/{uid_file} ──────────────────────────────

@router.delete("/calendars/{cal}/{uid_file}")
def caldav_delete_event(cal: str, uid_file: str, session: Session = Depends(get_db)):
    resolved = _resolve_cal(cal)
    if resolved is None:
        return Response(status_code=404)
    rid, is_block = resolved
    resource = session.get(Resource, rid)
    if not resource:
        return Response(status_code=404)

    uid = uid_file[:-4] if uid_file.endswith(".ics") else uid_file

    if is_block:
        block = _block_by_uid(session, rid, uid)
        if not block:
            return Response(status_code=404)
        session.delete(block)
        session.commit()
        return Response(status_code=204, headers=_DAV_HEADERS)

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
