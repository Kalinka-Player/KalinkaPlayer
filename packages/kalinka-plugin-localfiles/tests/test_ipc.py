"""Request and reply over a pair of queues: each reply answers its own request."""

import multiprocessing
import threading
import time

from kalinka_plugin_localfiles.ipc import RequestChannel, reply


def _answer_next(requests, replies, answer):
    """Answer the next request from another thread, as the far process would."""

    def serve():
        reply(replies, requests.get(timeout=5), answer)

    thread = threading.Thread(target=serve)
    thread.start()
    return thread


def test_a_request_gets_its_reply():
    requests, replies = multiprocessing.Queue(), multiprocessing.Queue()
    server = _answer_next(requests, replies, {"blob": b"vector"})

    answer = RequestChannel(requests, replies, timeout=5).ask({"query": "piano"})
    server.join()

    assert answer["blob"] == b"vector"


def test_the_request_reaches_the_far_side_unchanged():
    requests, replies = multiprocessing.Queue(), multiprocessing.Queue()
    seen = {}

    def serve():
        request = requests.get(timeout=5)
        seen.update(request)
        reply(replies, request, {})

    thread = threading.Thread(target=serve)
    thread.start()
    RequestChannel(requests, replies, timeout=5).ask({"query": "piano", "limit": 7})
    thread.join()

    assert seen["query"] == "piano" and seen["limit"] == 7


def test_no_reply_within_the_timeout_is_none():
    requests, replies = multiprocessing.Queue(), multiprocessing.Queue()
    started = time.monotonic()

    assert RequestChannel(requests, replies, timeout=0.2).ask({"query": "x"}) is None
    assert time.monotonic() - started < 2


def test_a_late_reply_does_not_answer_the_next_request():
    """The failure this guards: once one reply came late, every later request
    was answered with the reply meant for the one before it."""
    requests, replies = multiprocessing.Queue(), multiprocessing.Queue()

    assert (
        RequestChannel(requests, replies, timeout=0.2).ask({"query": "first"}) is None
    )
    reply(replies, requests.get(timeout=5), {"answer": "first"})

    server = _answer_next(requests, replies, {"answer": "second"})
    answer = RequestChannel(requests, replies, timeout=5).ask({"query": "second"})
    server.join()

    assert answer["answer"] == "second"


def test_a_late_reply_alone_is_still_no_answer():
    requests, replies = multiprocessing.Queue(), multiprocessing.Queue()

    assert (
        RequestChannel(requests, replies, timeout=0.2).ask({"query": "first"}) is None
    )
    reply(replies, requests.get(timeout=5), {"answer": "first"})

    assert (
        RequestChannel(requests, replies, timeout=0.5).ask({"query": "second"}) is None
    )


def test_concurrent_asks_each_get_their_own_reply():
    """An ask left running by a cancelled caller must not drop the reply the
    next caller is waiting for."""
    requests, replies = multiprocessing.Queue(), multiprocessing.Queue()
    channel = RequestChannel(requests, replies, timeout=5)

    def serve():
        for _ in range(2):
            request = requests.get(timeout=5)
            reply(replies, request, {"answer": request["query"]})

    server = threading.Thread(target=serve)
    server.start()
    answers = {}

    def ask(query):
        answers[query] = channel.ask({"query": query})["answer"]

    askers = [threading.Thread(target=ask, args=(q,)) for q in ("first", "second")]
    for asker in askers:
        asker.start()
    for asker in askers:
        asker.join()
    server.join()

    assert answers == {"first": "first", "second": "second"}


def test_waiting_for_an_earlier_ask_counts_against_the_timeout():
    requests, replies = multiprocessing.Queue(), multiprocessing.Queue()
    channel = RequestChannel(requests, replies, timeout=0.3)
    earlier = threading.Thread(target=channel.ask, args=({"query": "first"},))
    earlier.start()
    time.sleep(0.05)
    started = time.monotonic()

    assert channel.ask({"query": "second"}) is None
    earlier.join()

    assert time.monotonic() - started < 1
