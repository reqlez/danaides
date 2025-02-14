import asyncio
import os, sys, signal
import pandas as pd
import argparse

from time import sleep
from config import dotdict
from utils.db import eng, text, dnp
from utils.logger import logger, myself, Timer, printProgressBar, LEIF
from utils.ergo import get_node_info, get_genesis_block, NODE_API
from utils.aioreq import get_json_ordered
from sqlalchemy.exc import OperationalError
from plugins import prices, utxo, token
from ergo_python_appkit.appkit import ErgoAppKit, ErgoValue

#region INIT
# GLOBALs
PRETTYPRINT = False
VERBOSE = False
FETCH_INTERVAL = 1500
LINELEN = 100
PLUGINS = dotdict({
    'staking': True, 
    'utxo': True,
    'prices': True,
    'token': True,
})
TOKENS = 'tokens'
BOXES = 'boxes'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko', "Content-Type": "application/json"} # {'Content-Type': 'application/json'}
BLIPS = []
#endregion INIT

#region FUNCTIONS
# remove all inputs from current block
async def del_inputs(inputs: dict, unspent: dict, height: int=-1) -> dict:
    try:
        new = unspent
        for i in inputs:
            box_id = i['boxId']
            try:
                # new[box_id] = height
                new[box_id] = {
                    'height': height, # -1 indicates spent; see checkpoint (is_unspent in checkpoint_boxes table)
                    'nergs': 0
                }
            except Exception as e:
                BLIPS.append({'box_id': box_id, 'height': height, 'msg': f'cant remove'})
                if VERBOSE: logger.warning(f'cant find {box_id} at height {height} while removing from unspent {e}')
        return new

    except Exception as e:
        logger.error(f'ERR: find tokens {e}')
        return {}

# add all outputs from current block
async def add_outputs(outputs: dict, unspent: dict, height: int) -> dict:
    try:
        new = unspent
        for o in outputs:
            box_id = o['boxId']
            nergs = o['value']
            # amount = o['value']
            try:
                # new[box_id] = height
                new[box_id] = {
                    'height': height,
                    'nergs': nergs
                }
            except Exception as e:
                BLIPS.append({'box_id': box_id, 'height': height, 'msg': f'cant add'})
                if VERBOSE: logger.warning(f'{box_id} exists at height {height} while adding to unspent {e}')
        return new

    except Exception as e:
        logger.error(f'ERR: add outputs {e}')
        return {}

# upsert current chunk
async def checkpoint(height: int, unspent: dict, tokens: dict) -> None:
    try:
        # unspent
        if VERBOSE: logger.info(f'unspent: {unspent}')
        df = pd.DataFrame.from_dict({
            'box_id': list(unspent.keys()), 
            'height': [n['height'] for n in unspent.values()], # list(map(int, unspent.values())), # 'nergs': list(map(int, [v[0] for v in unspent.values()])), 
            'nerg': [n['nergs'] for n in unspent.values()],
            'is_unspent': [n['height']!=-1 for n in unspent.values()], # [b!=-1 for b in list(unspent.values())]
        })
        df.to_sql(f'checkpoint_{BOXES}', eng, if_exists='replace')
        if VERBOSE: logger.debug(f'checkpoint_boxes: {height}')

        # execute as transaction
        with eng.begin() as con:
            # remove spent
            sql = f'''
                with spent as (
                    select box_id
                    from checkpoint_{BOXES}
                    where is_unspent::boolean = false
                )
                delete from {BOXES} t
                using spent s
                where s.box_id = t.box_id
            '''
            if VERBOSE: logger.debug(sql)
            con.execute(sql)
            if VERBOSE: logger.debug(f'remove spent')

            # add unspent
            sql = f'''
                insert into {BOXES} (box_id, height, is_unspent, nerg)
                    select c.box_id, c.height, c.is_unspent, c.nerg
                    from checkpoint_{BOXES} c
                        left join {BOXES} b on b.box_id = c.box_id
                    where c.is_unspent::boolean = true
                        and b.box_id is null
                    ;
            '''
            if VERBOSE: logger.debug(sql)
            con.execute(sql)
            if VERBOSE: logger.debug(f'add unspent')

            sql = f'''
                insert into audit_log (height, service)
                values ({int(height)-1}, '{BOXES}')
            '''
            if VERBOSE: logger.debug(sql)
            con.execute(sql)      
            if VERBOSE: logger.debug(f'update audit log')  

        # tokens
        if tokens == {}:
            if VERBOSE: logger.debug('No tokens this block...')
        else:
            if PLUGINS.token:
                if VERBOSE: logger.debug(f'checkpoint tokens')
                resToken = await token.checkpoint(height, tokens, is_plugin=True, args=args)

        if VERBOSE: logger.debug(f'checkpoint complete.')

    except Exception as e:
        logger.error(f'ERR: checkpointing {e}')
        pass        

# performant API call
async def get_all(urls) -> dict:
    retries = 0
    res = {}
    while res == {}:
        try:
            res = await get_json_ordered(urls, HEADERS)
            retries = 5
        except Exception as e:
            retries += 1
            res = {}
            logger.warning(f'retry: {retries} ({e})')
            sleep(0.1)
            pass
    return res

# find the current block height
async def get_height(args, height: int=-1) -> int:
    if height > 0:
        # start from argparse
        logger.info(f'Starting at block; resetting tables {BOXES}, {TOKENS}: {height}...')
        with eng.begin() as con:
            sql = text(f'''delete from {TOKENS} where height >= {height}''')
            con.execute(sql)
            sql = text(f'''delete from {TOKENS} where height >= {height}''')
            con.execute(sql)
        logger.info(f'Height requested: {height}...')
        return height
    
    # start from genesis
    elif height == 0:
        logger.info(f'Staring at GENESIS, height 0...')
        return 0

    # where to begin reading blockchain
    else:
        # check existing height in audit_log
        sql = f'''
            select height 
            from audit_log 
            where service in ('{BOXES}')
            order by created_at desc 
            limit 1
        '''
        with eng.begin() as con:
            ht = con.execute(sql).fetchone()
        if ht is not None:
            height = ht['height']+1
            logger.info(f'''Existing boxes found, starting at {height}...''')
            return height

    # nada, start from scratch
    logger.info(f'''No height information, starting at genesis block...''')
    return 0

# handle primary functions: scan blocks sequentially; boxes, tokens, plugins
async def process(args, t, height: int=-1) -> dict:
    # find unspent boxes at current height
    node_info = get_node_info()
    current_height = node_info['fullHeight']
    unspent = {}
    tokens = {}

    # nothing found, start from beginning
    last_height = await get_height(args, height)
    if last_height == 0:
        res = get_genesis_block()
        genesis_blocks = res.json()
        for gen in genesis_blocks:
            box_id = gen['boxId']
            unspent[box_id] = {'height': 0, 'nergs': gen['value']} # height 0
        last_height = 1

    # lets gooooo...
    try:
        if last_height >= current_height:
            logger.warning('Already caught up...')

        # process blocks until caught up with current height
        while last_height < current_height:
            next_height = last_height+FETCH_INTERVAL
            if next_height > current_height:
                next_height = current_height
            batch_order = range(last_height, next_height)

            # find block headers
            suffix = f'''BLOCKS: {last_height}-{next_height} / {current_height}'''
            if PRETTYPRINT: printProgressBar(last_height, current_height, prefix=t.split(), suffix=f'{suffix}{" "*(LINELEN-len(suffix))}', length=50)
            else: 
                try: percent_complete = f'{100*last_height/current_height:0.2f}%'
                except: percent_complete= 0
                logger.info(f'{percent_complete}/{t.split()} {suffix}')
            urls = [[blk, f'{NODE_API}/blocks/at/{blk}'] for blk in batch_order]
            block_headers = await get_all(urls)

            # find transactions
            suffix = f'''TRANSACTIONS: {last_height}-{next_height} / {current_height}'''
            if PRETTYPRINT: printProgressBar(int(last_height+(FETCH_INTERVAL/3)), current_height, prefix=t.split(), suffix=f'{suffix}{" "*(LINELEN-len(suffix))}', length=50)
            else: 
                try: percent_complete = f'{100*int(last_height+(2*FETCH_INTERVAL/3))/current_height:0.2f}%'
                except: percent_complete= 0
                logger.info(f'{percent_complete}/{t.split()} {suffix}')
            urls = [[hdr[1], f'''{NODE_API}/blocks/{hdr[2][0]}/transactions'''] for hdr in block_headers if hdr[1] != 0]
            blocks = await get_all(urls)

            # recreate blockchain (must put together in order)
            suffix = f'''UNSPENT: {last_height}-{next_height} / {current_height}'''
            if PRETTYPRINT: printProgressBar(int(last_height+(2*FETCH_INTERVAL/3)), current_height, prefix=t.split(), suffix=f'{suffix}{" "*(LINELEN-len(suffix))}', length=50)
            else: 
                try: percent_complete = f'{100*int(last_height+(2*FETCH_INTERVAL/3))/current_height:0.2f}%'
                except: percent_complete= 0
                logger.info(f'{percent_complete}/{t.split()} {suffix}')
            for blk, transactions in sorted([[b[1], b[2]] for b in blocks]):
                for tx in transactions['transactions']:
                    unspent = await del_inputs(tx['inputs'], unspent)
                    unspent = await add_outputs(tx['outputs'], unspent, blk)
                    if PLUGINS.token:
                        tokens = await token.process(transactions['transactions'], tokens, blk, is_plugin=True, args=args)

            # checkpoint
            if VERBOSE: logger.debug('Checkpointing...')
            last_height += FETCH_INTERVAL
            suffix = f'Checkpoint at {next_height} (boxes: {len(unspent)}; tokens: {len(tokens)})...'            
            if PRETTYPRINT: printProgressBar(next_height, current_height, prefix=t.split(), suffix=f'{suffix}{" "*(LINELEN-len(suffix))}', length=50)
            else: 
                try: percent_complete = f'{100*next_height/current_height:0.2f}%'
                except: percent_complete= 0
                logger.warning(f'{percent_complete}/{t.split()} {suffix}')
            if len(unspent) > 0:
                await checkpoint(next_height, unspent, tokens)
            else:
                logger.error('ERR: 0 boxes found')
                
            unspent = {}
            tokens = {}

    except KeyboardInterrupt:
        logger.error('Interrupted.')
        try: sys.exit(0)
        except SystemExit: os._exit(0)         

    except Exception as e:
        logger.error(f'ERR: {myself()}; {e}')

    return {
        'current_height' : current_height,
        'blips': BLIPS
    }
#endregion FUNCTIONS

#region MAIN
# create common app framework
class App:
    def __init__(self):
        self.shutdown = False
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, signum, frame):
        print('Received:', signum)
        self.shutdown = True

    def init(self):
        logger.info("main.app:: Ready, go...")

    def stop(self):
        logger.info("main.app:: Fin.")

    # find all new currnet blocks
    async def process(self, args, height):
        # init timer
        t = Timer()
        t.start()

        # process
        res = await process(args, t, height)
        
        # timer
        sec = t.stop()
        logger.debug(f'main.app:: Danaides process took {sec:0.4f}s...')
        
        return res['current_height']

    # wait for new height
    def hibernate(self, last_height):
        try:
            current_height = last_height
            infinity_counter = 0
            t = Timer()
            t.start()

            # admin overview
            sql = f'''
                      select count(distinct address) as i, 0 as j,      'staking' as tbl from staking
                union select count(distinct address),      0,           'vesting'        from vesting
                union select count(*),                     max(height), 'utxos'          from utxos
                union select count(*),                     max(height), 'boxes'          from boxes
                union select count(*),                     max(height), 'tokens'         from tokens
            '''
            with eng.begin() as con:
                res = con.execute(sql) 
            logger.warning('\n\t'+'\n\t'.join([f'''main.hibernate:: {r['tbl']}: {r['i']} rows, {r['j']} height''' for r in res]))

            # chill for next block
            logger.debug('Hibernating...')
            while last_height == current_height:
                try:
                    inf = get_node_info()
                    current_height = inf['fullHeight']

                    if PRETTYPRINT: 
                        print(f'''\r({current_height}) {t.split()} Waiting for next block{'.'*(infinity_counter%4)}    ''', end = "\r")
                    else:
                        if infinity_counter%15 == 0:
                            logger.warning(f'''({current_height}) {t.split()} Waiting for next block...''')

                    infinity_counter += 1
                    sleep(1)

                except KeyboardInterrupt:
                    logger.debug('Interrupted.')
                    last_height = -1
                    try: sys.exit(0)
                    except SystemExit: os._exit(0)

            # cleanup audit log
            logger.debug('Cleanup audit log...')
            sql = f'''
                delete 
                -- select *
                from audit_log 
                where created_at < now() - interval '3 days';            
            '''
            with eng.begin() as con:
                con.execute(sql)

            sec = t.stop()
            logger.log(LEIF, f'Block took {sec:0.4f}s...')        

        except Exception as e:
            logger.error(f'ERR: {myself()}, {e}')

# handle command line interface directives
def cli():
    global PRETTYPRINT
    global VERBOSE
    global FETCH_INTERVAL
    global BOXES

    parser = argparse.ArgumentParser()
    
    parser.add_argument("-J", "--juxtapose", help="Alternative table name", default='boxes')
    parser.add_argument("-B", "--override", help="Process this box", default='')
    parser.add_argument("-H", "--height", help="Begin at this height", type=int, default=-1)
    parser.add_argument("-F", "--fetchinterval", help="Begin at this height", type=int, default=1500)
    parser.add_argument("-P", "--prettyprint", help="Progress bar vs. scrolling", action='store_true')
    parser.add_argument("-O", "--once", help="When complete, finish", action='store_true')
    parser.add_argument("-X", "--ignoreplugins", help="Only process boxes", action='store_true')
    parser.add_argument("-V", "--verbose", help="Be wordy", action='store_true')
    
    args = parser.parse_args()

    if args.ignoreplugins:
        logger.warning('Ignoring plugins...')
        for p in PLUGINS: p = False
    if args.juxtapose != 'boxes': logger.warning(f'Using alt boxes table: {args.juxtapose}...')        
    if args.height != -1: logger.warning(f'Starting at height: {args.height}...')
    if args.prettyprint: logger.warning(f'Pretty print...')
    if args.once: logger.warning(f'Processing once, then exit...')
    if args.verbose: logger.warning(f'Verbose...')
    if args.fetchinterval != 1500: logger.warning(f'Fetch interval: {args.fetchinterval}...')

    PRETTYPRINT = args.prettyprint
    VERBOSE = args.verbose
    FETCH_INTERVAL = args.fetchinterval    
    BOXES = ''.join([i for i in args.juxtapose if i.isalpha()]) # only-alpha tablename

    return args

if __name__ == '__main__':
    
    # sanity check    
    try: 
        eng.execute('select 1')
    except OperationalError as e:
        logger.warning(f'unable to init db, waiting 5 seconds and trying again')
        sleep(5)
        try: 
            eng.execute('select 1')
        except Exception as e: 
            logger.error(f'ERR: unable to connect to database, has API service initialized objects; {e}')
            try: sys.exit(0)
            except SystemExit: os._exit(0)            

    # setup
    args = cli()
    
    # create app
    app = App()
    app.init()
    
    # process loop
    height = args.height # first time
    while not app.shutdown:
        try:
            # build boxes, tokens tables
            height = asyncio.run(app.process(args, height))

            ## -----------
            ## - PLUGINS -
            ## -----------
            ##
            ## These calls create a current view of all blockchain unspent transactions.
            ##
            ## Some tables can be updated incrementally, which is very quick; others are built from views in a way that the
            ##   update process does not block api queries, and the result table is very performant.
            ##            

            # UTXOS - process first; builds utxos table
            if PLUGINS.utxo:
                logger.warning('main:: PLUGIN: Utxo...')
                # call last_height-1 as node info returns currently in progress block height, and plugin needs to process the previous block
                asyncio.run(utxo.process(is_plugin=True, args=args))

            # PRICES - builds prices table
            if PLUGINS.prices:
                logger.warning('main:: PLUGIN: Prices...')
                asyncio.run(prices.process(is_plugin=True, args=args))

            # DROP-N-POP
            for tbl in ['staking', 'vesting', 'assets', 'balances']:
                logger.info(f'main:: {tbl.upper()}...')
                res = asyncio.run(dnp(tbl))
                logger.debug(f'''main:: {tbl.upper()} compete ({res['row_count_before']}/{res['row_count_after']} before/after rows)''')

            # quit or wait for next block
            if args.once:
                app.stop()
                try: sys.exit(0)
                except SystemExit: os._exit(0)            
            else:
                app.hibernate(height)

        except KeyboardInterrupt:
            logger.debug('Interrupted.')
            app.stop()
            try: sys.exit(0)
            except SystemExit: os._exit(0)            

        except Exception as e:
            logger.error(f'ERR: {myself()}, {e}')

    # fin
    app.stop()
    try: sys.exit(0)
    except SystemExit: os._exit(0)            
#region MAIN