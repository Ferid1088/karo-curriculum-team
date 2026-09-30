import pytest

from kcteam.lessons import content_grade, classification_errors, FormatInvalid, export_response


def test_level_is_clamped_to_canonical_range():
    c = dict(first_contact_grade=5, target_grade=6)
    assert [content_grade(c, g) for g in [1, 5, 6, 13]] == [5, 5, 6, 6]
    with pytest.raises(FormatInvalid):
        content_grade(dict(first_contact_grade=7, target_grade=2), 1)


def test_valid_schema_does_not_override_canonical_metadata():
    c = dict(first_contact_grade=5, target_grade=6)
    spec = {'id': 'karo-adaptiv-v1'}
    assert classification_errors({'konzept': {'klasse_von': 1, 'klasse_bis': 1}}, c, spec)
    assert not classification_errors({'konzept': {'klasse_von': 5, 'klasse_bis': 6}}, c, spec)


def test_old_wrongly_classified_export_is_not_delivered():
    class DB:
        def concept(self, cid):
            return dict(status='approved', version=1, first_contact_grade=5, target_grade=6)
    row = dict(id=1, concept_id='fractions', concept_version=1, format_id='karo-adaptiv-v1',
               status='ready', lesson={'konzept': {'klasse_von': 1, 'klasse_bis': 1}})
    _, out = export_response(row, DB())
    assert out['status'] == 'unavailable' and 'lesson' not in out
