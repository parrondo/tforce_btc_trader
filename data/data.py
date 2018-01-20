import time, json, re
from enum import Enum
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy import text
import os

config_json = json.load(open(os.path.dirname(__file__) + '/../config.json'))
DB = config_json['DB_HISTORY'].split('/')[-1]
engine = create_engine(config_json['DB_HISTORY'])
engine_live = create_engine(config_json['DB_HISTORY_LIVE'])
engine_runs = create_engine(config_json['DB_RUNS'])

# From connecting source file, import engine and run the following code. Need each connection to be separate
# (see https://stackoverflow.com/questions/3724900/python-ssl-problem-with-multiprocessing)
# conn = engine.connect()


class Exchange(Enum):
    GDAX = 'gdax'
    KRAKEN = 'kraken'


EXCHANGE = Exchange.GDAX

# Methods for imputing NaN. F=ffill, B=bfill, Z=zero. Generally we want volume/size-based features to be 0-filled
# (indicating no trading during this blank period) and prices to ffill (maintain where the price left off). Right?
F = 0
B = 1
Z = 2

if 'alex' in DB or DB == 'dbk0cfbk3mfsb6':
    tables = [
    {
        'name': 'exch_ticker_coinbase_usd',
        'ts': 'last_update',
        'cols': dict(last_trade_price=F, last_trade_size=Z, bid_price=F, bid_size=Z, bid_num_orders=Z, ask_price=F,
                     ask_size=Z, ask_num_orders=Z)
    },
    {
        'name': 'exch_ticker_kraken_usd',
        'ts': 'last_update',
        'cols': dict(last_trade_price=F, last_trade_lot_volume=Z, ask_price=F, ask_lot_volume=Z, bid_price=F,
                     bid_lot_volume=Z, volume=Z, volume_last24=Z, vwap=Z, vwap_last24=Z, number_trades=Z,
                     number_trades_last24=Z, low=F, low_last24=F, high=F, high_last24=F, open_price=F),
        'ohlcv': dict(open='open_price', high='high', low='low', close='last_trade_price', volume='volume'),
    },
    ]
    target = 'exch_ticker_coinbase_usd_last_trade_price'

    # order of tables counts - first one should be the table containing the target
    if EXCHANGE == Exchange.KRAKEN:
        tables[0], tables[1] = tables[1], tables[0]
        target = 'exch_ticker_kraken_usd_last_trade_price'

elif 'kaggle' in DB:
    tables = [
    {
        'name': 'coinbase',
        'ts': 'timestamp',
        'cols': dict(open=F, high=F, low=F, close=F, volume_btc=Z, volume_currency=Z, weighted_price=Z),
        'ohlcv': dict(open='open', high='high', low='low', close='close', volume='volume_currency')
    },
    {
        'name': 'coincheck',
        'ts': 'timestamp',
        'cols': dict(open=F, high=F, low=F, close=F, volume_btc=Z, volume_currency=Z, weighted_price=Z),
        'ohlcv': dict(open='open', high='high', low='low', close='close', volume='volume_currency')
    }
    ]
    target = 'coinbase_close'

def get_tables(arbitrage=True):
    return tables if arbitrage else [tables[0]]

def n_cols(indicators=False, arbitrage=True):
    cols = 0
    tables_ = get_tables(arbitrage)
    for t in tables_:
        cols += len(t['cols'])
        if indicators and 'ohlcv' in t:
            cols += 4  # This is defined in btc_env._get_indicators()
    # Extra 3 cols (self.cash, self.value, self.repeats) are added in downstream dense
    return cols


row_count = 0
already_asked = False
def count_rows(conn, arbitrage=True):
    global row_count, already_asked
    if row_count:
        return row_count  # cached
    elif already_asked:
        time.sleep(5)  # give the first go a chance to cache a value
    already_asked = True

    row_count = db_to_dataframe(conn, just_count=True, arbitrage=arbitrage)
    print('row_count: ', row_count)
    return row_count


def _db_to_dataframe_ohlc(conn, limit='ALL', offset=0, just_count=False, arbitrage=True):
    # 600, 300, 1800
    if just_count:
        select = 'select count(*) over () '
        limit, offset = 1, 0
    else:
        select = """
        select 
          g.open_price as g_open, g.high_price as g_high, g.low_price as g_low, g.close_price as g_close, g.volume as g_volume,
          o.open_price as o_open, o.high_price as o_high, o.low_price as o_low, o.close_price as o_close, o.volume as o_volume"""
    query = f"""
    {select}
    from ohlc_gdax as g 
    inner join ohlc_okcoin as o on g.close_time=o.close_time
    where g.period='60' and o.period='60'
    order by g.close_time::integer desc
    limit {limit} offset {offset}
    """
    if just_count:
        return conn.execute(query).fetchone()[0]

    return pd.read_sql_query(query, conn).iloc[::-1].ffill()

def _db_to_dataframe_main(conn, limit='ALL', offset=0, just_count=False, arbitrage=True, last_timestamp=False):
    """Fetches all relevant data in database and returns as a Pandas dataframe"""
    tables_ = get_tables(arbitrage)

    if just_count:
        query = 'select count(*) over ()'
    else:
        query = 'select ' + ', '.join(
            ', '.join(f"{t['name']}.{c} as {t['name']}_{c}" for c in t['cols'])
            for t in tables_
        )

    # Currently matching main-table to nearest secondary-tables' time (https://stackoverflow.com/questions/28839524/join-two-tables-based-on-nearby-timestamps)
    # Could also group by intervals (10s, 60s, etc) https://gis.stackexchange.com/a/127874/105932
    first = tables_[0]
    for i, table in enumerate(tables_):
        name, ts = table['name'], table['ts']
        if i == 0:
            query += f" from {name}"
            continue
        prior = tables_[i-1]
        query += f"""
            left join lateral (
              select {', '.join(c for c in table['cols'])}
              from {name}
              where {name}.{ts} <= {prior['name']}.{prior['ts']}
              order by {name}.{ts} desc
              limit 1 
            ) {name} on true
            """

    if just_count:
        query += " limit 1"
        return conn.execute(query).fetchone()[0]

    order_field = f"{first['name']}.{first['ts']}" if len(tables_) > 1 else first['ts']
    query += f" order by {order_field} desc limit {limit} offset {offset}"

    # order by date DESC (for limit to cut right), then reverse again (so old->new)
    df = pd.read_sql_query(query, conn).iloc[::-1]
    for t in tables_:
        for k, method in t['cols'].items():
            fill = {'value': 0} if method == Z else {'method': 'ffill' if method == F else 'bfill'}
            col_name = f"{t['name']}_{k}"
            df[col_name] = df[col_name].fillna(fill)
    df = df.astype('float64')

    if last_timestamp:
        # Save away last-timestamp (used in LIVE mode to inform how many new steps are added between polls
        query = f"select {first['ts']} from {first['name']} order by {first['ts']} desc limit 1"
        last_timestamp = conn.execute(query).fetchone()[first['ts']]
        return df, last_timestamp
    return df


db_to_dataframe = _db_to_dataframe_ohlc if 'coins' in DB else _db_to_dataframe_main


def fetch_more(conn, last_timestamp, arbitrage):
    # FIXME this count approach won't work in inner-join-arbitrage mode
    t = tables[0]
    query = f"select count(*) as ct from {t['name']} where {t['ts']} > :last_timestamp"
    n_new = conn.execute(text(query), last_timestamp=last_timestamp).fetchone()['ct']
    if n_new == 0:
        return None, 0, last_timestamp
    new_data, latest_timestamp = db_to_dataframe(conn, limit=n_new, arbitrage=arbitrage, last_timestamp=True)
    return new_data, n_new, latest_timestamp


def setup_runs_table():
    conn_runs = engine_runs.connect()
    conn_runs.execute("""
        create table if not exists runs
        (
            id serial not null,
            hypers jsonb not null,
            advantage_avg double precision not null,
            advantages double precision[],
            actions double precision[],
            prices double precision[],
            uniques double precision[],
            flag varchar(16),
            agent varchar(64) default 'ppo_agent'::character varying not null
        );
    """)