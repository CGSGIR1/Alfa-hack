"""Editorial provenance of the original organizer-supplied Alfa-Bank documents.

Use exact known document identities, never a publisher-name or ID-prefix guess.
The badge describes the source; it does not imply endorsement of our conclusions.
"""

ORGANIZER_ALFA_IDS = frozenset({
    'alfa_7m26_2026_09_07',
    'alfa_consumption_2026_08_05',
    'alfa_gold_2026_08',
    'alfa_inflation_2026_09_02',
    'alfa_macro1h_2026_07_31',
    'alfa_policy_2026_09_11',
    'alfa_steels_2021_04_15',
    'alfa_strategy_2026_07_22',
})

ORGANIZER_ALFA_LABEL = 'Аналитика Альфа-Банка · материал организаторов'


def source_provenance(document_id):
    """Also supports old saved reports that predate explicit provenance metadata."""
    organizer = document_id in ORGANIZER_ALFA_IDS
    return dict(origin='organizer' if organizer else 'external',
                provided_by_organizer=organizer,
                provider='Альфа-Банк' if organizer else None,
                label=ORGANIZER_ALFA_LABEL if organizer else None)
