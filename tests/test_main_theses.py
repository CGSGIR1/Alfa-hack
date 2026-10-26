"""Main-event comparison safeguards. All vectors are synthetic; no model/API."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.integration.main_theses import compare_main, compare_main_with_context, main_thesis, numbers


class MainThesisTests(unittest.TestCase):
    def test_repeated_background_is_not_selected_as_the_main_event(self):
        fragments = [dict(id='background',thesis='Минфин покупает валюту.'),
                     dict(id='main',thesis='ЦБ установил новый курс доллара.')]
        result = main_thesis([1,0],fragments,{'background':np.array([0,1]),'main':np.array([1,0])})
        self.assertEqual(result['chunk_id'],'main')

    def test_matching_background_does_not_qualify_different_main_events(self):
        titles={'a':[1,0], 'b':[0,1]}
        vectors={'a-main':[1,0],'b-main':[0,1],'shared':[1,1]}
        a=dict(id='a',date='2026-10-24',main=dict(chunk_id='a-main',text='ЦБ установил курс.'))
        b=dict(id='b',date='2026-10-24',main=dict(chunk_id='b-main',text='Минфин покупает валюту.'))
        result=compare_main(a,b,titles,vectors)
        self.assertEqual(result['status'],'below_candidate_threshold')
        self.assertFalse(result['suppress'])

    def test_new_numbers_are_an_update_even_with_identical_vectors(self):
        a=dict(id='a',date='2026-10-24',main=dict(chunk_id='a-main',text='МТС планирует привлечь 10 млрд рублей.'))
        b=dict(id='b',date='2026-10-24',main=dict(chunk_id='b-main',text='МТС привлекла 30 млрд рублей.'))
        result=compare_main(a,b,{'a':[1,0],'b':[1,0]}, {'a-main':[1,0],'b-main':[1,0]})
        self.assertEqual(result['status'],'possible_update')
        self.assertEqual(result['numeric_differences'],dict(only_a=['10'],only_b=['30']))
        self.assertFalse(result['suppress'])

    def test_possible_same_event_is_only_a_candidate_not_a_deletion(self):
        a=dict(id='a',date='2026-10-24',main=dict(chunk_id='a-main',text='Компания открыла завод.'))
        b=dict(id='b',date='2026-10-24',main=dict(chunk_id='b-main',text='Открыт завод компании.'))
        result=compare_main(a,b,{'a':[1,0],'b':[1,0]}, {'a-main':[1,0],'b-main':[1,0]})
        self.assertEqual(result['status'],'possible_same_event')
        self.assertFalse(result['suppress'])
        b['date']='2026-10-20'
        self.assertEqual(compare_main(a,b,{'a':[1,0],'b':[1,0]}, {'a-main':[1,0],'b-main':[1,0]})['status'],'below_candidate_threshold')

    def test_missing_or_invalid_evidence_is_not_silently_used(self):
        self.assertIsNone(main_thesis([1,0],[dict(id='f',thesis='')],{}))
        with self.assertRaises(ValueError):
            main_thesis([1,0],[dict(id='f',thesis='Событие')],{})
        with self.assertRaises(ValueError):
            main_thesis([1,0],[dict(id='f',thesis='Событие')],{'f':[float('nan'),1]})
        self.assertEqual(numbers('14,00% и 0.00% в 2026 году'),['0','14','2026'])

    def test_context_profile_checks_selected_main_instead_of_shared_background(self):
        a=dict(id='a',date='2026-10-24',main=dict(chunk_id='a-main',text='Главное событие.'))
        b=dict(id='b',date='2026-10-24',main=dict(chunk_id='b-main',text='Главное событие.'))
        titles={'a':[1,0],'b':[1,0]};theses={'a-main':[1,0],'b-main':[1,0]}
        context={'a-main':[1,0],'b-main':[0,1],'shared':[1,0]}
        self.assertEqual(compare_main_with_context(a,b,titles,theses,context)['status'],'below_candidate_threshold')
        context['b-main']=[1,0]
        result=compare_main_with_context(a,b,titles,theses,context)
        self.assertEqual(result['status'],'possible_same_event')
        self.assertFalse(result['suppress'])


if __name__=='__main__':
    unittest.main()
