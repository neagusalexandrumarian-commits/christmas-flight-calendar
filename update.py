"""Refresh bounded, rotating routes; reuse completed searches from the same UTC day."""
import os, json, datetime, sys
from pathlib import Path
from decimal import Decimal
from apify_client import ApifyClient
ROOT = Path(__file__).parent
FILE = ROOT / 'data/prices.json'
DEST = json.loads((ROOT / 'data/destinations.json').read_text())
TOKEN = os.environ.get('APIFY_TOKEN')
if not TOKEN:
    sys.exit('APIFY_TOKEN not configured; preserving previous data.')
client = ApifyClient(TOKEN)
state = json.loads(FILE.read_text())
state.setdefault('prices', {})
state.setdefault('errors', {})
today = datetime.datetime.now(datetime.timezone.utc).date()
if today > datetime.date(2026, 12, 30):
    print('Christmas 2026 date window has ended; no searches performed.')
    sys.exit(0)
actor = client.actor('kuezi/flight-offers-api')
# Fail closed if we cannot check previous attempts; do not spend again blindly.
recent = actor.runs().list(limit=100, desc=True, started_after=today.isoformat()).items
cached = []
for item in recent:
    run = client.run(item.id).get()
    if run and run.status == 'SUCCEEDED':
        record = client.key_value_store(run.default_key_value_store_id).get_record('INPUT')
        cached.append(((record or {}).get('value') or {}, run))
max_calls = min(max(int(os.environ.get('MAX_DAILY_CALLS', '5')), 0), 5)
slot = today.toordinal() % 6
origin = ['OTP', 'BBU'][slot // 3]
duration = [4, 5, 6][slot % 3]
batch_start = ((today.toordinal() // 6) % 2) * 5
attempts = len(recent)
successes = 0
for dest in DEST[batch_start:batch_start + max_calls]:
    airport = 'BSL' if dest['name'] == 'Strasbourg' else dest['airport']
    key = f"{origin}-{airport}-{duration}"
    try:
        inp = {'endpoint': 'cheapest-date', 'originLocationCode': origin,
               'destinationLocationCode': airport, 'departureDate': '2026-12-18,2026-12-26',
               'duration': duration, 'oneWay': False, 'nonStop': True, 'adults': 1,
               'currencyCode': 'EUR', 'country': 'ro', 'language': 'en'}
        run = next((r for previous, r in cached if all(previous.get(k) == v for k, v in inp.items())), None)
        if run:
            print(key, 'reusing completed search', run.id)
        else:
            if attempts >= 5:
                raise RuntimeError('Daily five-run limit reached')
            attempts += 1
            run = actor.call(run_input=inp, run_timeout=datetime.timedelta(seconds=240),
                wait_duration=datetime.timedelta(seconds=300), max_total_charge_usd=Decimal('0.03'),
                restart_on_error=False)
            if not run or run.status != 'SUCCEEDED':
                raise RuntimeError('Provider run did not succeed')
            cached.append((inp, run))
        record = client.key_value_store(run.default_key_value_store_id).get_record('RESPONSE')
        response = (record or {}).get('value') or {}
        rows = response.get('data') if isinstance(response, dict) else None
        if not isinstance(rows, list):
            raise ValueError('Unexpected provider data format')
        saved = 0
        for row in rows:
            try:
                out, ret = row['departureDate'], row['returnDate']
                price = float(row['price']['total'])
                d1, d2 = datetime.date.fromisoformat(out), datetime.date.fromisoformat(ret)
                if not (datetime.date(2026, 12, 18) <= d1 and d2 <= datetime.date(2026, 12, 30)
                        and (d2-d1).days == duration and 0 < price < float('inf')):
                    continue
                state['prices'][f"{dest['name']}|{out}|{ret}|{origin}"] = {
                    'price': price, 'currency': 'EUR', 'source': 'Apify / Google Flights-derived',
                    'checked_at': (run.finished_at or run.started_at).isoformat(),
                    'origin': origin, 'destination_airport': airport, 'baggage': 'UNKNOWN',
                    'direct': 'UNVERIFIED', 'airline': 'Not supplied by cheapest-date endpoint',
                    'note': 'Indicative base round-trip fare. Nonstop requested; cabin suitcase and nonstop status need booking-site verification.'}
                saved += 1
            except (KeyError, ValueError, TypeError):
                continue
        state['errors'].pop(key, None)
        state['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        state['notice'] = 'Live indicative quotes loaded. Availability and baggage require verification.'
        successes += 1
        print(key, 'saved', saved)
    except Exception as e:
        state['errors'][key] = type(e).__name__ + ': Provider query failed; inspect provider dashboard.'
        print(key, type(e).__name__, file=sys.stderr)
    finally:
        FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + chr(10))
if not successes:
    sys.exit('No route refreshed successfully; previous quotes preserved.')
