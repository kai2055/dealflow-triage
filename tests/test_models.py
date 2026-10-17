"""Tests for the pitch record: it accepts good data dn refuses bad data"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from triage.models import PitchState, RawPitch


VALID_HASH = "a" * 64
NOW = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)

def test_valid_pitch_is_accepted():
    pitch = RawPitch(source="folder", raw_hash=VALID_HASH, received_at=NOW)
    assert pitch.state == PitchState.RECEIVED
    assert pitch.pitch_id is not None
    assert pitch.attachments == []



def test_time_wothout_zone_is_refused():
    with pytest.raises(ValidationError):
        RawPitch(source="folder", raw_hash=VALID_HASH, received_at=datetime(2026, 10, 17, 20, 0))


def test_broken_hash_is_refused():
    with pytest.raises(ValidationError):
        RawPitch(source="folder", raw_hash="abc123", received_at=NOW)

def test_mispelled_field_isrefused():
    with pytest.raises(ValidationError):
        RawPitch(source="folder", raw_hash=VALID_HASH, received_at=NOW, subjcet="Hello")

def test_unknown_source_is_refused():
    with pytest.raises(ValidationError):
        RawPitch(source="linkedin", raw_hash=VALID_HASH, received_at=NOW)
