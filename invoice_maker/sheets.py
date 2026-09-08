"""Google Sheets access, restricted to read-only scope."""
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from requests import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .domain import parse_rows


class SourceError(ValueError):
    pass


class SheetsSource:
    def __init__(self, credentials_path, spreadsheet_id):
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
        self.session = AuthorizedSession(credentials)
        self.session.mount('https://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])))
        self.spreadsheet_id = spreadsheet_id
        self.base = f'https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id, safe="")}'

    def _get(self, url, params=None):
        try:
            response = self.session.get(url, params=params, timeout=(10, 30))
            if response.status_code in (401, 403):
                raise SourceError('Google не разрешает чтение. Проверьте доступ служебного аккаунта и включение Google Sheets API.')
            if response.status_code == 404:
                raise SourceError('Google-таблица не найдена или недоступна служебному аккаунту.')
            response.raise_for_status()
            return response.json()
        except SourceError:
            raise
        except (RequestException, ValueError):
            raise SourceError('Не удалось прочитать Google-таблицу. Попробуйте ещё раз через минуту.') from None

    def metadata(self):
        return self._get(self.base, {'fields': 'spreadsheetId,properties(title,timeZone),sheets(properties(sheetId,title,index,hidden,sheetType))'})

    def list_sheets(self):
        metadata = self.metadata()
        return [s['properties'] for s in reversed(metadata.get('sheets', [])) if s['properties'].get('sheetType', 'GRID') == 'GRID']

    def fetch_sheet(self, sheet_id):
        metadata = self.metadata()
        selected = next((s['properties'] for s in metadata.get('sheets', []) if s['properties']['sheetId'] == int(sheet_id)), None)
        if not selected:
            raise SourceError('Лист удалён. Обновите список месяцев.')
        range_name = "'" + selected['title'].replace("'", "''") + "'!A:F"
        values = self._get(self.base + '/values/' + quote(range_name, safe=''), {'valueRenderOption': 'UNFORMATTED_VALUE'})
        return {'title': selected['title'], 'timezone': metadata['properties'].get('timeZone', 'Europe/Moscow'), 'rows': parse_rows(values.get('values', []))}
