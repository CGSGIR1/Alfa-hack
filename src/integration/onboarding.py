"""Prepare a CSV company once, without overwriting existing profiles or facts."""
import os
import re

from news_analytics.common import read_json, require
from news_analytics.validation import company as validate_company
from .settings import path


def company_id(value):
    require(isinstance(value, str), 'Введите ID компании из CSV, например U_9250 или 9250.')
    value = value.strip().upper()
    require(re.fullmatch(r'(?:U_)?[0-9]{1,20}', value) is not None,
            'Введите ID компании из CSV, например U_9250 или 9250.')
    return value if value.startswith('U_') else 'U_' + value


def prepared_profile(config, identifier):
    """A prepared JSON can be imported even when the CSV/LLM is unavailable."""
    for locations in ('upload_folders', 'inputs'):
        folder = config.get(locations, {}).get('companies')
        if not folder or not folder.is_dir():
            continue
        for target in sorted(folder.rglob('*.json')):
            try:
                doc = read_json(target)
            except (ValueError, UnicodeError):
                continue
            if isinstance(doc, dict) and doc.get('subject_id') == identifier and doc.get('schema_version'):
                return validate_company(doc)
    return None


def csv_directory(config):
    directory = path(os.environ.get('DATA_DIR') or config.get('company_profiles', {}).get('csv_directory', 'data'))
    required = ('df_Uid_data_hack.csv', 'df_Uid_payment_hack.csv', 'df_Fid_Uid_link_hack.csv')
    require(all((directory / name).is_file() for name in required),
            'Не найдены исходные CSV компаний, платежей и связей. Укажите папку с ними в DATA_DIR.')
    return directory


def build_profile(config, identifier):
    from .profiles import company_json
    try:
        return company_json(identifier, csv_directory(config), refine=True, require_llm=True)
    except KeyError as error:
        raise ValueError('Компания ' + identifier + ' не найдена в исходных CSV.') from error
