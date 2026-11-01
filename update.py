"""Refresh a rotating portion of the calendar; never invent fares."""
import os, json, datetime, sys
from pathlib import Path
from apify_client import ApifyClient
ROOT = Path(__file__).parent
FILE = ROOT / 'data/prices.json'
DEST = json.loads((ROOT / 'data/destinations.json').read_text())
TOKEN = os.environ.get('APIFY_TOKEN')
if not TOKEN:
    print('APIFY_TOKEN not configured; preserving previous data.')
    sys.exit(0)
client = ApifyClient(TOKEN)
state = json.loads(FILE.read_text())
state.setdefault('prices', {})
state.setdefault('errors', {})
today = datetime.datetime.now(datetime.timezone.utc).date()
if today > datetime.date(2026, 12, 30):
    sys.exit('Christmas 2026 date window has ended.')
max_calls = min(max(int(os.environ.get('MAX_DAILY_CALLS', '1')), 0), 10)
slot = today.toordinal() % 6
origin = ['OTP', 'BBU'][slot // 3]
duration = [4, 5, 6][slot % 3]
start = '2026-12-18,2026-12-26'
for dest in DEST[:max_calls]:
    airport = 'BSL' if dest['name'] == 'Strasbourg' else dest['airport']
    key = f"{origin}-{airport}-{duration}"
    try:
        inp = {'endpoint': 'cheapest-date', 'originLocationCode': origin,
               'destinationLocationCode': airport, 'departureDate': start,
               'duration': duration, 'adults': 1, 'currencyCode': 'EUR',
               'country': 'ro', 'language': 'en'}
        run = client.actor('kuezi/flight-offers-api').call(run_input=inp, timeout_secs=240)
        record = client.key_value_store(run['defaultKeyValueStoreId']).get_record('RESPONSE')
        response = (record or {}).get('value') or {}
        rows = response.get('data', []) if isinstance(response, dict) else []
        if not isinstance(rows, list):
            raise ValueError('Unexpected provider data format')
        saved = 0
        for row in rows:
            try:
                out, ret = row['departureDate'], row['returnDate']
                price = float(row['price']['total'])
                d1, d2 = datetime.date.fromisoformat(out), datetime.date.fromisoformat(ret)
                if not (datetime.date(2026, 12, 18) <= d1 and d2 <= datetime.date(2026, 12, 30)
                        and (d2-d1).days == duration and price > 0):
                    continue
                state['prices'][f"{dest['name']}|{out}|{ret}|{origin}"] = {
                    'price': price, 'currency': 'EUR', 'source': 'Apify / Google Flights-derived',
                    'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'origin': origin, 'destination_airport': airport, 'baggage': 'UNKNOWN',
                    'direct': 'UNVERIFIED', 'airline': 'Not supplied by cheapest-date endpoint',
                    'note': 'Indicative base round-trip fare. Cabin suitcase and nonstop status NOT verified.'}
                saved += 1
            except (KeyError, ValueError, TypeError):
                continue
        state['errors'].pop(key, None)
        print(key, 'saved', saved)
    except Exception as e:
        state['errors'][key] = type(e).__name__ + ': Provider query failed; inspect provider dashboard.'
        print(key, type(e).__name__, file=sys.stderr)
    finally:
        state['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + chr(10))
