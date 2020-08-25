import argparse
import time
import humanize
import multiprocessing
import bz2
import io
import os
import os.path
import re
import queue
import traceback

import chess
import chess.pgn

import maia_chess_backend

logging_delay = 30 # in seconds


def main():

    parser = argparse.ArgumentParser(description='process PGN file with stockfish annotaions into a csv file', formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('input', help='input PGNs')
    parser.add_argument('outputDir', help='output CSVs dir')

    parser.add_argument('--pool', type=int, help='number of simultaneous jobs running per file', default = 30)
    parser.add_argument('--allow_non_sf', help='Allow games with no stockfish info', default = False, action="store_true")
    #parser.add_argument('--debug', help='DEBUG MODE', default = False, action="store_true")
    #parser.add_argument('--readers', type=int, help='number of simultaneous reader running per inputfile', default = 24)
    parser.add_argument('--queueSize', type=int, help='Max number of games to cache', default = 1000)

    args = parser.parse_args()

    maia_chess_backend.printWithDate(f"Starting CSV conversion of {args.input} writing to {args.outputDir}")

    os.makedirs(args.outputDir, exist_ok=True)

    name = os.path.basename(args.input).split('.')[0]
    outputName = os.path.join(args.outputDir, f"{name}.csv.bz2")
    #names[n] = (name, outputName)

    maia_chess_backend.printWithDate(f"Loading file: {name}")
    maia_chess_backend.printWithDate(f"Starting main loop")

    tstart = time.time()
    with multiprocessing.Manager() as manager:
        with multiprocessing.Pool(args.pool) as workers_pool, multiprocessing.Pool(3) as io_pool:
            pgnReader, gameReader, writer, unproccessedQueue, resultsQueue = processPGN(args.input, name, outputName, args.queueSize, args.pool, args.allow_non_sf, manager, workers_pool, io_pool)

            maia_chess_backend.printWithDate(f"Done loading Queues in {humanize.naturaldelta(time.time() - tstart)}, waiting for reading to finish")

            cleanup(pgnReader, gameReader, writer)

def processPGN(gamesPath, inputName, outputName, queueSize, poolSize, allow_non_sf, manager, workers_pool, io_pool):
    unproccessedQueue = manager.Queue(queueSize)
    resultsQueue = manager.Queue(queueSize)

    readers = []
    for _ in range(poolSize - 1):
        reader = workers_pool.apply_async(gamesConverter, (unproccessedQueue, resultsQueue, allow_non_sf))
        readers.append(reader)
    maia_chess_backend.printWithDate(f"{inputName} Started {len(readers)} readers", flush = True)
    pgnReader = io_pool.apply_async(readerWorker, (gamesPath, unproccessedQueue, resultsQueue, inputName, len(readers)))
    maia_chess_backend.printWithDate(f"{inputName} loader created")

    writer = io_pool.apply_async(writerWorker, (outputName, resultsQueue, len(readers), inputName))
    maia_chess_backend.printWithDate(f"{inputName} Started writer for: {inputName}", flush = True)

    return pgnReader, readers, writer, unproccessedQueue, resultsQueue

def gamesConverter(inputQueue, outputQueue, allow_non_sf):
    #maia_chess_backend.printWithDate("Converter created")
    while True:
        try:
            #print('qsize', inputQueue.qsize())
            dat = inputQueue.get()
        except queue.Empty:
            break
        if dat == 'kill':
            outputQueue.put('kill', True, 1000)
            break
        else:
            try:
                s = maia_chess_backend.gameToCSVlines(dat, allow_non_sf = allow_non_sf)
            except maia_chess_backend.NoStockfishEvals:
                pass
            except:
                maia_chess_backend.printWithDate('error:')
                maia_chess_backend.printWithDate(dat)
                maia_chess_backend.printWithDate(traceback.format_exc())
                raise
            else:
                if len(s) > 0:
                    lines = '\n'.join(s) + '\n'
                    outputQueue.put(lines.encode('utf8'), True, 1000)
    maia_chess_backend.printWithDate("Received shutdown signal to Converter", flush = True)

def readerWorker(inputPath, unproccessedQueue, resultsQueue, name, num_readers):
    tstart = time.time()
    gamesFile = maia_chess_backend.LightGamesFile(inputPath, just_games = True)
    try:
        tLast = time.time()
        for i, (_, gs) in enumerate(gamesFile):
            unproccessedQueue.put(gs, True, 1000)
            if i % 1000 == 0 and  time.time() - tLast > logging_delay:
                tLast = time.time()
                maia_chess_backend.printWithDate(f"{name} Loaded {i} games, input queue depth: {unproccessedQueue.qsize()}, ouput queue depth: {resultsQueue.qsize()}", flush = True)
    except (EOFError, StopIteration):
        pass

    maia_chess_backend.printWithDate(f"{name} Done loading Queue in {humanize.naturaldelta(time.time() - tstart)}, sending kills")
    for i in range(num_readers):
        #maia_chess_backend.printWithDate(f"Putting kill number {i} in queue")
        unproccessedQueue.put('kill', True, 100)


def writerWorker(outputFile, inputQueue, num_readers, name):
    i = -1
    num_kill_remaining = num_readers
    tstart = time.time()
    maia_chess_backend.printWithDate("Writer created")
    with bz2.open(outputFile, 'wb') as f:
        maia_chess_backend.printWithDate(f"Created: {outputFile}")
        f.write((','.join(maia_chess_backend.full_csv_header) + '\n').encode('utf8'))
        tLast = time.time()
        while True:
            try:
                dat = inputQueue.get()
            except queue.Empty:
                #Should never happen
                break
            try:
                f.write(dat)
            except TypeError:
                if dat == 'kill':
                    num_kill_remaining -= 1
                    if num_kill_remaining <= 0:
                        break
                else:
                    raise
            else:
                i += 1
                if i % 1000 == 0 and  time.time() - tLast > logging_delay:
                    tLast = time.time()
                    maia_chess_backend.printWithDate(f"{name} Written {i} games in {humanize.naturaldelta(time.time() - tstart)}, doing {(i + 1) /(time.time() - tstart):.0f} games a second", flush = True)
    maia_chess_backend.printWithDate("Received shutdown signal to writer")
    maia_chess_backend.printWithDate(f"Done a total of {i} games in {humanize.naturaldelta(time.time() - tstart)}")

def cleanup(pgnReaders, gameReaders, writers):

    #time.sleep(10)
    while len(gameReaders) > 0:
        for i in range(len(gameReaders)):
            #maia_chess_backend.printWithDate(f"Checking {i} of {len(gameReaders)}", flush = True)
            try:
                gameReaders[i].get(1)
            except multiprocessing.TimeoutError:
                pass
            else:
                del gameReaders[i]
                break
    maia_chess_backend.printWithDate(f"Done processing")
    pgnReaders.get()
    maia_chess_backend.printWithDate(f"Done reading")
    writers.get()
    maia_chess_backend.printWithDate(f"Done cleanup")

if __name__ == '__main__':
    main()
