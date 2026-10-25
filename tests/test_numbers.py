"""Exact standalone numbers, long digit IDs and final-session conversion."""
from pathlib import Path
import random
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as m
from test_targeted_fix import Engine, feed, until
from test_focus_symbols import pump, pump_until

DIGITS = 'zero one two three four five six seven eight nine'.split()
ONES = DIGITS + 'ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']
SCALES = ['', 'thousand', 'million', 'billion', 'trillion', 'quadrillion', 'quintillion',
          'sextillion', 'septillion', 'octillion', 'nonillion', 'decillion', 'undecillion',
          'duodecillion', 'tredecillion', 'quattuordecillion', 'quindecillion', 'sexdecillion',
          'septendecillion', 'octodecillion', 'novemdecillion', 'vigintillion']


def cardinal(number, use_and=False):
    if number == 0:
        return 'zero'
    parts, index = [], 0
    while number:
        number, group = divmod(number, 1000)
        if group:
            chunk = []
            hundreds, rest = divmod(group, 100)
            if hundreds:
                chunk += [ONES[hundreds], 'hundred']
                if rest and use_and:
                    chunk.append('and')
            if rest < 20:
                if rest:
                    chunk.append(ONES[rest])
            else:
                chunk.append(TENS[rest // 10])
                if rest % 10:
                    chunk.append(ONES[rest % 10])
            if SCALES[index]:
                chunk.append(SCALES[index])
            parts.append(' '.join(chunk))
        index += 1
    return ' '.join(reversed(parts))


@pytest.mark.parametrize('spoken,expected', [
    ('five zero', '50'), ('twenty', '20'), ('zerozerofive', '005'),
    ('zero zero five', '005'), ('fivezero', '50'), ('ZeroZeroFive', '005'),
    ('zero0five', '005'), ('five 0', '50'), ('5 zero', '50'), ('one 234', '1234'),
    ('zero', '0'), ('one', '1'), ('nine', '9'), ('oh five', '05'),
    ('one two three four five six seven eight nine zero', '1234567890'),
    ('twenty-three', '23'), ('one hundred and five', '105'),
    ('one thousand two hundred and thirty four', '1234'),
    ('a hundred', '100'), ('a thousand and five', '1005'),
    ('one lakh twenty three thousand four hundred fifty six', '123456'),
    ('two crore five lakh fifty thousand', '20550000'),
    ('one thousand million', '1000000000'), ('zero thousand', '0'),
    ('negative five zero', '-50'), ('minus zero zero five', '-005'),
    ('point zero zero five', '0.005'), ('one point zero zero five', '1.005'),
    ('negative twenty point zero five', '-20.05'),
    ('My PIN is zerozerofive.', 'My PIN is 005.'),
    ('I have twenty files and five folders.', 'I have 20 files and 5 folders.'),
    ('one and two', '1 and 2'), ('one, two, three.', '1, 2, 3.'),
    ('one hundred and a half dollars', '$100.5'),
    ('two and a half dollars', '$2.5'),
    ('five zero dollars', '$50'), ('fivezerorupees', 'fivezerorupees'),
    ('five zero rupees', '₹50'), ('zerozerofive percent', '005%'),
    ('two minus three', '2 - 3'), ('twenty plus five equals twenty five', '20 + 5 = 25'),
    ('negative twenty degrees Celsius', '-20°C'),
    ('one centillion one', '1' + '0' * 302 + '1'),
    ('one googol and one', '1' + '0' * 99 + '1'),
])
def test_requested_and_extended_number_examples(spoken, expected):
    assert m.normalize_dictation_text(spoken) == expected


@pytest.mark.parametrize('number', range(100))
def test_every_number_below_one_hundred(number):
    assert m.normalize_dictation_text(cardinal(number)) == str(number)


RNG = random.Random(509005)
GENERATED = [RNG.randrange(10 ** 64) for _ in range(240)]


@pytest.mark.parametrize('number', GENERATED)
def test_generated_large_cardinals_are_exact(number):
    assert m.normalize_dictation_text(cardinal(number, use_and=bool(number % 2))) == str(number)


@pytest.mark.parametrize('length', [1, 2, 3, 10, 23, 24, 25, 40, 41, 100, 1000, 4300, 4301, 10000, 25000])
@pytest.mark.parametrize('fused', [False, True])
def test_digit_sequences_have_no_fixed_length_limit_and_keep_leading_zeros(length, fused):
    expected = ('005123456789' * ((length + 11) // 12))[:length]
    spoken = ('' if fused else ' ').join(DIGITS[int(char)] for char in expected)
    assert m.normalize_dictation_text(spoken) == expected


def test_very_long_already_numeric_id_is_unchanged():
    value = '005' + '1234567890' * 1000
    assert m.normalize_dictation_text(value) == value


def test_repeated_scales_exceed_python_integer_string_digit_limit_without_rounding():
    assert m.normalize_dictation_text('one ' + 'thousand ' * 1500).strip() == '1' + '0' * 4500


def test_long_decimal_keeps_every_fraction_digit_and_trailing_zeros():
    fraction = '0051234567890' * 400
    spoken = 'zero point ' + ' '.join(DIGITS[int(char)] for char in fraction)
    assert m.normalize_dictation_text(spoken) == '0.' + fraction


def test_large_currency_and_minor_units_are_not_rounded_by_decimal_context():
    major = '1234567890' * 10
    spoken = ' '.join(DIGITS[int(char)] for char in major)
    assert m.normalize_dictation_text(spoken + ' dollars') == '$' + major
    assert m.normalize_dictation_text(spoken + ' dollars and fifty cents') == '$' + major + '.50'


@pytest.mark.parametrize('text', [
    'someone anyone everyone stone alone none', 'This is the one I wanted.',
    'No one came.', 'one of a kind', 'Help one another.', 'Oh no!',
    'zeroone@example.com', 'https://one.example.com/twenty', 'www.twenty.com',
    'release_005 fivezero_suffix product123', '2026-10-25', '50-5',
    '0044 123456 789012', '1,234.050',
])
def test_words_identifiers_urls_and_existing_numeric_formatting_are_preserved(text):
    assert m.normalize_dictation_text(text) == text


def test_explicit_number_containing_symbols_still_take_precedence():
    for alias, expected in m.SPOKEN_SYMBOLS.items():
        assert m.normalize_dictation_text(alias) == expected


def test_spoken_digit_conversion_across_precomputed_recording_parts_is_atomic(qt):
    engine = Engine()
    values = iter(['zero zero', 'five six', 'seven eight'])
    def transcribe(*args, **kwargs):
        engine.model.calls.append(None)
        return next(values)
    engine.model.transcribe = transcribe
    heard = []
    engine.heard.connect(heard.append)
    try:
        engine.toggle_record()
        feed(engine, seconds=17)
        until(lambda: len(engine.model.calls) >= 2 and not engine.busy)
        pump(qt, 20)
        assert not engine.typed and not heard
        engine.toggle_record()
        pump_until(qt, lambda: engine.pending == 0 and len(engine.typed) == 1)
        assert engine.typed == ['005678 ']
        assert heard == ['005678']
    finally:
        engine.shutdown()
        engine._segment_thread.join(1)
        engine._transcribe_thread.join(1)


def test_long_digit_conversion_remains_linear_enough_for_stop_path():
    spoken = 'zerozerofive' * 10000
    started = time.perf_counter()
    assert m.normalize_dictation_text(spoken) == '005' * 10000
    assert time.perf_counter() - started < 4
