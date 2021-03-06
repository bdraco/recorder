"""Models for SQLAlchemy."""
from datetime import datetime
import json
import logging

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_AREA_ID,
    ATTR_ENTITY_PICTURE,
    ATTR_FRIENDLY_NAME,
    ATTR_HIDDEN,
    ATTR_ICON,
    ATTR_DEVICE_CLASS,
    ATTR_EDITABLE,
    EVENT_STATE_CHANGED
)

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    distinct,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm.session import Session

from homeassistant.core import Context, Event, EventOrigin, State, split_entity_id
from homeassistant.helpers.json import JSONEncoder
import homeassistant.util.dt as dt_util

# SQLAlchemy Schema
# pylint: disable=invalid-name
Base = declarative_base()

SCHEMA_VERSION = 7

# Ideally we would also filter out ATTR_UNIT_OF_MEASUREMENT and ATTR_FRIENDLY_NAME, however the history api needs this
# for the computeHistory UI functionality.  Unfortunately that means it has to be stored
# for every state change.   In the future we might be able to refactor so the system can
# fetch the current unit of measurement for the entity and mix it in to avoid storing it
# for every state change
KEYS_TO_FILTER_FROM_DB = [ATTR_ENTITY_ID, ATTR_AREA_ID, ATTR_ENTITY_PICTURE, ATTR_HIDDEN, ATTR_ICON, ATTR_DEVICE_CLASS, ATTR_EDITABLE]

_LOGGER = logging.getLogger(__name__)


class Events(Base):  # type: ignore
    """Event history data."""

    __tablename__ = "events"
    event_id = Column(Integer, primary_key=True)
    event_type = Column(String(32), index=True)
    event_data = Column(Text)
    origin = Column(String(32))
    time_fired = Column(DateTime(timezone=True), index=True)
    created = Column(DateTime(timezone=True), default=datetime.utcnow)
    context_id = Column(String(36), index=True)
    context_user_id = Column(String(36), index=True)
    # context_parent_id = Column(String(36), index=True)

    @staticmethod
    def from_event(event):
        """Create an event database object from a native event."""
        return Events(
            event_type=event.event_type,
            event_data=json.dumps(_filter_state_change_event_data(event.data) if event.event_type == EVENT_STATE_CHANGED else event.data, cls=JSONEncoder),
            origin=str(event.origin),
            time_fired=event.time_fired,
            context_id=event.context.id,
            context_user_id=event.context.user_id,
            # context_parent_id=event.context.parent_id,
        )

    def to_native(self):
        """Convert to a natve HA Event."""
        context = Context(id=self.context_id, user_id=self.context_user_id)
        try:
            return Event(
                self.event_type,
                json.loads(self.event_data),
                EventOrigin(self.origin),
                _process_timestamp(self.time_fired),
                context=context,
            )
        except ValueError:
            # When json.loads fails
            _LOGGER.exception("Error converting to event: %s", self)
            return None


class States(Base):  # type: ignore
    """State change history."""

    __tablename__ = "states"
    state_id = Column(Integer, primary_key=True)
    domain = Column(String(64))
    entity_id = Column(String(255), index=True)
    state = Column(String(255))
    attributes = Column(Text)
    event_id = Column(Integer, ForeignKey("events.event_id"), index=True)
    last_changed = Column(DateTime(timezone=True), default=datetime.utcnow)
    last_updated = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    created = Column(DateTime(timezone=True), default=datetime.utcnow)
    context_id = Column(String(36), index=True)
    context_user_id = Column(String(36), index=True)
    # context_parent_id = Column(String(36), index=True)

    __table_args__ = (
        # Used for fetching the state of entities at a specific time
        # (get_states in history.py)
        Index("ix_states_entity_id_last_updated", "entity_id", "last_updated"),
    )

    @staticmethod
    def from_event(event):
        """Create object from a state_changed event."""
        entity_id = event.data["entity_id"]
        state = event.data.get("new_state")

        dbstate = States(
            entity_id=entity_id,
            context_id=event.context.id,
            context_user_id=event.context.user_id,
            # context_parent_id=event.context.parent_id,
        )

        # State got deleted
        if state is None:
            dbstate.state = ""
            dbstate.domain = split_entity_id(entity_id)[0]
            dbstate.attributes = "{}"
            dbstate.last_changed = event.time_fired
            dbstate.last_updated = event.time_fired
        else:
            dbstate.domain = state.domain
            dbstate.state = state.state
            dbstate.attributes = json.dumps(_filter_attributes(dict(state.attributes)), cls=JSONEncoder)
            dbstate.last_changed = state.last_changed
            dbstate.last_updated = state.last_updated

        return dbstate

    def to_native(self):
        """Convert to an HA state object."""
        context = Context(id=self.context_id, user_id=self.context_user_id)
        try:
            return State(
                self.entity_id,
                self.state,
                json.loads(self.attributes),
                _process_timestamp(self.last_changed),
                _process_timestamp(self.last_updated),
                context=context,
                # Temp, because database can still store invalid entity IDs
                # Remove with 1.0 or in 2020.
                temp_invalid_id_bypass=True,
            )
        except ValueError:
            # When json.loads fails
            _LOGGER.exception("Error converting row to state: %s", self)
            return None


class RecorderRuns(Base):  # type: ignore
    """Representation of recorder run."""

    __tablename__ = "recorder_runs"
    run_id = Column(Integer, primary_key=True)
    start = Column(DateTime(timezone=True), default=datetime.utcnow)
    end = Column(DateTime(timezone=True))
    closed_incorrect = Column(Boolean, default=False)
    created = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (Index("ix_recorder_runs_start_end", "start", "end"),)

    def entity_ids(self, point_in_time=None):
        """Return the entity ids that existed in this run.

        Specify point_in_time if you want to know which existed at that point
        in time inside the run.
        """
        session = Session.object_session(self)

        assert session is not None, "RecorderRuns need to be persisted"

        query = session.query(distinct(States.entity_id)).filter(
            States.last_updated >= self.start
        )

        if point_in_time is not None:
            query = query.filter(States.last_updated < point_in_time)
        elif self.end is not None:
            query = query.filter(States.last_updated < self.end)

        return [row[0] for row in query]

    def to_native(self):
        """Return self, native format is this model."""
        return self


class SchemaChanges(Base):  # type: ignore
    """Representation of schema version changes."""

    __tablename__ = "schema_changes"
    change_id = Column(Integer, primary_key=True)
    schema_version = Column(Integer)
    changed = Column(DateTime(timezone=True), default=datetime.utcnow)


def _filter_attributes(unfiltered_dict):
    """Remove attribute fields that are unlikely to be needed in the database"""
    return dict([(key, val) for key, val in 
           unfiltered_dict.items() if key not in KEYS_TO_FILTER_FROM_DB]) 

def _filter_state_change_event_data(unfiltered_dict):
    """Remove duplicate data and attributes that are unlikely to be needed
    in the database from an event"""
    filtered_dict = unfiltered_dict.copy()
    for key in ("old_state", "new_state"):
        if key in filtered_dict and isinstance(filtered_dict[key],State):
            filtered_dict[key] = filtered_dict[key].as_dict()
            filtered_dict[key]['attributes'] = _filter_attributes(filtered_dict[key]['attributes'])
    return filtered_dict 

def _process_timestamp(ts):
    """Process a timestamp into datetime object."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return dt_util.UTC.localize(ts)

    return dt_util.as_utc(ts)
