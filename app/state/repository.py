from datetime import datetime, timezone
import re
import unicodedata
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, or_
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response
from .models import State


_STATE_ALIASES = {
    "ap": "andhrapradesh",
    "ar": "arunachalpradesh",
    "as": "assam",
    "br": "bihar",
    "cg": "chhattisgarh",
    "ga": "goa",
    "gj": "gujarat",
    "gujrath": "gujarat",
    "hr": "haryana",
    "hp": "himachalpradesh",
    "himachalpardesh": "himachalpradesh",
    "jh": "jharkhand",
    "ka": "karnataka",
    "kl": "kerala",
    "la": "ladakh",
    "mh": "maharashtra",
    "maharastra": "maharashtra",
    "ml": "meghalaya",
    "mn": "manipur",
    "mp": "madhyapradesh",
    "mz": "mizoram",
    "nl": "nagaland",
    "od": "odisha",
    "orissa": "odisha",
    "pb": "punjab",
    "py": "puducherry",
    "pondicherry": "puducherry",
    "rj": "rajasthan",
    "sk": "sikkim",
    "tn": "tamilnadu",
    "tg": "telangana",
    "ts": "telangana",
    "telengana": "telangana",
    "tr": "tripura",
    "up": "uttarpradesh",
    "uk": "uttarakhand",
    "uttaranchal": "uttarakhand",
    "wb": "westbengal",
    "dl": "delhi",
    "delhinct": "delhi",
    "nctdelhi": "delhi",
    "nctofdelhi": "delhi",
    "an": "andamanandnicobarislands",
    "jk": "jammukashmir",
    "jkandl": "jammukashmir",
    "dd": "damananddiu",
    "dn": "dadranagarhaveli",
}


def _normalize_state_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", text.strip().lower())


def _canonical_state_text(value: str) -> str:
    normalized = _normalize_state_text(value)
    return _STATE_ALIASES.get(normalized, normalized)


def _state_label(state: State) -> str:
    return str(getattr(state, "stateName", "") or getattr(state, "stateCode", "") or "").strip()


def _state_candidates(state: State):
    seen = set()
    for raw in (
        getattr(state, "stateName", "") or "",
        getattr(state, "stateCode", "") or "",
    ):
        label = str(raw).strip()
        if not label:
            continue
        candidate = _canonical_state_text(label)
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield label, candidate


def _active_states_query(db: Session):
    return db.query(State).filter(
        or_(State.deletedInd == False, State.deletedInd.is_(None))
    )


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (0 if char_a == char_b else 1)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def resolve_state_id(
    db: Session,
    state_id: Optional[int] = None,
    state_name: Optional[str] = None,
) -> Optional[int]:
    if state_id is not None:
        try:
            resolved_id = int(state_id)
        except (TypeError, ValueError):
            resolved_id = None
        if resolved_id is not None:
            exists = _active_states_query(db).filter(State.stateId == resolved_id).first()
            if exists:
                return resolved_id

    target = _canonical_state_text(state_name or "")
    if not target:
        return None

    states = _active_states_query(db).all()
    best_id: Optional[int] = None
    best_score = -1.0

    for state in states:
        try:
            candidate_id = int(state.stateId)
        except (TypeError, ValueError):
            continue

        for label, candidate in _state_candidates(state):
            if not candidate:
                continue

            if candidate == target:
                return candidate_id

            if candidate in target or target in candidate:
                score = 0.9
            else:
                distance = _levenshtein(target, candidate)
                score = 1.0 - (distance / max(len(target), len(candidate), 1))

            if score > best_score:
                best_score = score
                best_id = candidate_id

    if best_id is not None and best_score >= 0.5:
        return best_id
    return None

def get_all(db: Session, filters, order_ascending, order_property, page_index, page_size) -> dict:
    query = _active_states_query(db)
    query = apply_filters(query, State, filters)
    query = apply_ordering(query, State, order_property, order_ascending)
    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)
    rows = query.all()
    return {
        "count": total,
        "list": [{"stateId": r.stateId, "stateName": r.stateName, "stateCode": r.stateCode,
                  "isActive": r.isActive, "countryId": r.countryId, "userProfileId": r.userProfileId}
                 for r in rows],
        "parameters": None
    }


def get_by_id(db: Session, state_id: int) -> Optional[dict]:
    entity = _active_states_query(db).filter(State.stateId == state_id).first()
    if not entity:
        return None
    return {"stateId": entity.stateId, "stateName": entity.stateName, "stateCode": entity.stateCode,
            "isActive": entity.isActive, "countryId": entity.countryId, "userProfileId": entity.userProfileId}


def create(db: Session, data) -> int:
    entity = State(
        stateName=data.stateName,
        stateCode=data.stateCode,
        isActive=data.isActive,
        countryId=data.countryId,
        userProfileId=data.userProfileId,
        createdDate=datetime.now(timezone.utc),
        deletedInd=False,
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity.stateId


def update(db: Session, data) -> Optional[int]:
    entity = db.query(State).filter(State.stateId == data.stateId).first()
    if not entity:
        return None
    entity.stateName = data.stateName
    entity.stateCode = data.stateCode
    entity.isActive = data.isActive
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return entity.stateId


def delete(db: Session, state_id: int) -> bool:
    entity = db.query(State).filter(State.stateId == state_id).first()
    if not entity:
        return False
    entity.deletedInd = True
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True
