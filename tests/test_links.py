"""Transfer links: the rules of links.LinkService (with a clock moved by hand), and the links used over
HTTP, where every refusal gets the same answer."""

import logging
import threading

import pytest
from helpers import TOKENS, Clock, access_lines, generate, gif, png, reference, share, size_of, submit, wait_for, write_json

from pixdrop.links import REFUSAL, Link, LinkRefused, LinkService, Target

LIFETIME = 3600
UPLOAD = Target(job_id="1793456280412_a9f3", upload=True)
DOWNLOAD = Target(job_id="1793456280412_a9f3", kind="images", index=1, size="medium")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def links(clock):
    return LinkService(LIFETIME, clock)


def test_a_link_serves_its_client_and_transfer(links, clock):
    secret, expires_at = links.create("alice", "10.0.0.5", DOWNLOAD)
    assert expires_at == clock.now + LIFETIME
    link = links.check(secret, "10.0.0.5", upload=False)
    assert (link.user, link.target, link.client, link.expires_at) == ("alice", DOWNLOAD, "10.0.0.5", expires_at)


def test_only_from_the_address_that_asked_for_it(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    with pytest.raises(LinkRefused, match="^link for 10.0.0.5 used from 10.0.0.6$"):
        links.check(secret, "10.0.0.6", upload=True)


def test_only_for_its_kind_of_transfer(links):
    upload, _ = links.create("alice", "10.0.0.5", UPLOAD)
    download, _ = links.create("alice", "10.0.0.5", DOWNLOAD)
    with pytest.raises(LinkRefused, match="^upload link used for a download$"):
        links.check(upload, "10.0.0.5", upload=False)
    with pytest.raises(LinkRefused, match="^download link used for an upload$"):
        links.check(download, "10.0.0.5", upload=True)


def test_unknown_secrets_are_refused(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    for wrong in ("", "x", secret[:-1], secret + "x", secret.upper(), "' OR 1=1 --"):
        with pytest.raises(LinkRefused, match="^unknown link$"):
            links.check(wrong, "10.0.0.5", upload=True)


def test_links_expire(links, clock):
    secret, expires_at = links.create("alice", "10.0.0.5", UPLOAD)
    clock.now = expires_at - 0.001
    links.check(secret, "10.0.0.5", upload=True)
    clock.now = expires_at
    with pytest.raises(LinkRefused, match="^expired link$"):
        links.check(secret, "10.0.0.5", upload=True)


def test_expired_links_are_forgotten_when_links_are_made(links, clock):
    old, _ = links.create("alice", "10.0.0.5", UPLOAD)
    clock.now += LIFETIME
    links.create("alice", "10.0.0.5", UPLOAD)
    with pytest.raises(LinkRefused, match="^unknown link$"):  # no longer "expired": gone
        links.check(old, "10.0.0.5", upload=True)


def test_a_transfer_checked_in_time_may_end_after_the_expiry(links, clock):
    secret, expires_at = links.create("alice", "10.0.0.5", UPLOAD)
    link = links.check(secret, "10.0.0.5", upload=True)  # when the upload starts
    clock.now = expires_at + 60  # a slow upload
    with links.using(link):
        pass
    with pytest.raises(LinkRefused):
        links.check(secret, "10.0.0.5", upload=True)


def test_a_completed_transfer_uses_the_link_up(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    with links.using(links.check(secret, "10.0.0.5", upload=True)):
        pass
    with pytest.raises(LinkRefused, match="^unknown link$"):
        links.check(secret, "10.0.0.5", upload=True)


@pytest.mark.parametrize("error", [ValueError("bad image"), KeyboardInterrupt(), SystemExit(1)])
def test_a_failed_transfer_leaves_the_link_valid(links, error):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    link = links.check(secret, "10.0.0.5", upload=True)
    with pytest.raises(type(error)), links.using(link):
        raise error
    assert links.check(secret, "10.0.0.5", upload=True) == link


def test_two_transfers_at_once_cannot_both_use_a_link(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    first = links.check(secret, "10.0.0.5", upload=True)
    second = links.check(secret, "10.0.0.5", upload=True)  # checked while the first one is uploading
    with links.using(first), pytest.raises(LinkRefused, match="^link used up meanwhile$"), links.using(second):
        pass
    with pytest.raises(LinkRefused, match="^link used up meanwhile$"), links.using(second):  # and after it
        pass


def test_of_concurrent_transfers_one_succeeds(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    link = links.check(secret, "10.0.0.5", upload=True)
    barrier = threading.Barrier(16)
    succeeded = []

    def transfer():
        barrier.wait()
        try:
            with links.using(link):
                succeeded.append(True)
        except LinkRefused:
            pass

    threads = [threading.Thread(target=transfer) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert succeeded == [True]


def test_a_download_is_used_up_once_sent(links):
    secret, _ = links.create("alice", "10.0.0.5", DOWNLOAD)
    link = links.check(secret, "10.0.0.5", upload=False)
    links.check(secret, "10.0.0.5", upload=False)  # checking does not use it
    links.use_up(link)
    with pytest.raises(LinkRefused, match="^unknown link$"):
        links.check(secret, "10.0.0.5", upload=False)
    links.use_up(link)  # twice is harmless


def test_secrets_are_random_and_kept_only_as_hashes(links):
    secrets = {links.create("alice", "10.0.0.5", UPLOAD)[0] for _ in range(200)}
    assert len(secrets) == 200
    assert all(len(secret) >= 43 for secret in secrets)  # 256 bits, URL-safe base64
    assert all(set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") for secret in secrets)
    kept = repr(vars(links))
    assert not any(secret in kept for secret in secrets)


def test_the_refusal_says_nothing_of_the_reason():
    for reason in ("expire", "work once", "address"):
        assert reason in REFUSAL  # the answer lists every possibility instead
    assert REFUSAL.startswith("no such link")


# --- over HTTP ---


def make_link(app, target: Target, user="alice", client="testclient") -> str:
    secret, _ = app.state.links.create(user, client, target)
    return f"/api/links/{secret}"


def refused(response) -> bool:
    return (response.status_code, response.json()) == (404, {"detail": REFUSAL})


def test_a_download_link_works_once_and_without_the_token(client, anonymous, app, data_dir):
    job = generate(client)
    link = make_link(app, Target(job_id=job["id"]))
    response = anonymous.get(link)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == f'inline; filename="{job["id"]}.png"'
    assert response.content == (data_dir / "outputs" / "alice" / f"{job['id']}.png").read_bytes()
    assert refused(anonymous.get(link))


def test_download_links_for_the_medium_size_and_for_references(client, anonymous, app):
    job = generate(client, references=[("big.gif", gif(1000, 500))])
    medium = anonymous.get(make_link(app, Target(job_id=job["id"], size="medium")))
    assert (medium.headers["content-type"], size_of(medium.content)) == ("image/png", (512, 512))
    assert medium.headers["content-disposition"] == f'inline; filename="{job["id"]}_medium.png"'
    original = anonymous.get(make_link(app, Target(job_id=job["id"], kind="references")))
    assert (original.headers["content-type"], original.content) == ("image/gif", gif(1000, 500))
    small = anonymous.get(make_link(app, Target(job_id=job["id"], kind="references", size="medium")))
    assert size_of(small.content) == (512, 256)


def test_a_download_that_fails_leaves_the_link_valid(client, anonymous, app, data_dir):
    job = generate(client)
    path = data_dir / "outputs" / "alice" / f"{job['id']}.png"
    link = make_link(app, Target(job_id=job["id"]))
    path.rename(path.with_suffix(".bak"))
    missing = anonymous.get(link)
    assert (missing.status_code, missing.json()) == (404, {"detail": "the image file is missing on the server"})
    path.with_suffix(".bak").rename(path)
    assert anonymous.get(link).status_code == 200
    assert refused(anonymous.get(link))


def test_a_link_always_serves_the_whole_file(client, anonymous, app):
    # A Range request would let the same link serve the file several times, in parts.
    job = generate(client)
    link = make_link(app, Target(job_id=job["id"]))
    whole = anonymous.get(link, headers={"Range": "bytes=0-7"})
    assert (whole.status_code, len(whole.content)) == (200, job["images"][0]["bytes"])
    assert refused(anonymous.get(link))


def test_an_upload_link_starts_the_job(client, anonymous, app):
    job = submit(client, reference_count=2).json()
    link = make_link(app, Target(job_id=job["id"], upload=True))
    response = anonymous.post(link, files=[reference("front.png", png()), reference("back.png", png(32, 32))])
    assert response.status_code == 200
    uploaded = response.json()
    assert (uploaded["status"], [picture["filename"] for picture in uploaded["references"]]) == ("generating", ["1_front.png", "2_back.png"])
    assert uploaded["upload_url"] is None and uploaded["references"][0]["url"] is None  # no API URLs: they need the token
    assert refused(anonymous.post(link, files=[reference("again.png", png())]))
    assert wait_for(client, job["id"])["status"] == "completed"


def test_a_refused_upload_leaves_the_link_valid(client, anonymous, app):
    job = submit(client, reference_count=1).json()
    link = make_link(app, Target(job_id=job["id"], upload=True))
    bad = anonymous.post(link, files=[reference("notes.txt", b"hello")])
    assert (bad.status_code, bad.json()) == (400, {"detail": "notes.txt: unsupported image type: use PNG, JPEG, WebP, GIF or SVG"})
    too_many = anonymous.post(link, files=[reference("a.png", png()), reference("b.png", png())])
    assert too_many.status_code == 400
    assert anonymous.post(link, data={"x": "1"}).status_code == 400  # no file
    assert anonymous.post(link, files=[reference("a.png", png())]).json()["status"] == "generating"
    assert refused(anonymous.post(link, files=[reference("a.png", png())]))


def test_an_upload_link_for_a_job_that_no_longer_waits(client, anonymous, app):
    job = generate(client)
    link = make_link(app, Target(job_id=job["id"], upload=True))
    response = anonymous.post(link, files=[reference("a.png", png())])
    assert (response.status_code, response.json()) == (409, {"detail": "the job is completed: reference images can be added only while it is pending"})


def test_the_link_is_checked_before_the_upload_is_read(anonymous):
    assert refused(anonymous.post("/api/links/no-such-link", data={"prompt": "x"}))  # no file: would be refused otherwise too
    assert refused(anonymous.post("/api/links/no-such-link", content=b"--garbage", headers={"Content-Type": "multipart/form-data; boundary=x"}))


def test_every_refusal_gets_the_same_answer(client, anonymous, app, data_dir, caplog, clock):
    caplog.set_level(logging.INFO)
    job = generate(client)
    pending = submit(client, reference_count=1).json()
    download = make_link(app, Target(job_id=job["id"]))
    upload = make_link(app, Target(job_id=pending["id"], upload=True))
    used = make_link(app, Target(job_id=job["id"]))
    anonymous.get(used)
    elsewhere = make_link(app, Target(job_id=job["id"]), client="10.0.0.9")
    attempts = [
        anonymous.get("/api/links/never-made"),
        anonymous.get(used),
        anonymous.get(elsewhere),  # made for another address
        share(client, client=("10.0.0.9", 50000)).get(download),  # used from another address
        anonymous.get(upload),  # an upload link, for a download
        anonymous.post(download, files=[reference("a.png", png())]),  # a download link, for an upload
    ]
    assert all(refused(response) for response in attempts)
    reasons = [line.rsplit(": ", 1)[1] for line in access_lines(caplog) if " /api/links/*** 404 " in line]
    assert reasons == [
        "unknown link", "unknown link", "link for 10.0.0.9 used from testclient", "link for testclient used from 10.0.0.9",
        "upload link used for a download", "download link used for an upload",
    ]  # fmt: skip

    app.state.links = LinkService(LIFETIME, clock)  # links made from now on follow the test's clock
    expiring = make_link(app, Target(job_id=job["id"]))
    clock.now += LIFETIME
    assert refused(anonymous.get(expiring))
    assert access_lines(caplog)[-1].endswith(": expired link")
    assert anonymous.get(download).status_code == 404  # the old service, and its links, are gone


def test_links_of_removed_users_stop_working(client, anonymous, app, data_dir, caplog):
    caplog.set_level(logging.INFO)
    job = generate(client)
    link = make_link(app, Target(job_id=job["id"]))
    write_json(data_dir / "users.json", {"users": {"bob": TOKENS["bob"]}})
    assert refused(anonymous.get(link))
    assert access_lines(caplog)[-1].endswith(": link of 'alice', who is no longer a user")
    write_json(data_dir / "users.json", {"users": TOKENS})
    assert anonymous.get(link).status_code == 200  # never used up meanwhile


def test_a_link_gives_only_its_users_image(client, bob, anonymous, app):
    job = generate(client)
    stolen = make_link(app, Target(job_id=job["id"]), user="bob")  # bob's link, for alice's job id
    response = anonymous.get(stolen)
    assert (response.status_code, response.json()) == (404, {"detail": "no such job"})


def test_link_types():
    assert Link.__dataclass_fields__.keys() == {"hash", "user", "target", "client", "expires_at"}  # no secret kept
