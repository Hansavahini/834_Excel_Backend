"""
The client's existing roster, loaded once per file instead of once per loop.

Why this exists. Identity resolution used to issue between two and five queries
for every INS loop — member id, then dependant-by-family, then SSN fingerprint,
then the same three again on the Subscriber/Dependant side in roster_sync. On a
six thousand loop file that is roughly thirty thousand round trips to answer a
question whose entire input set fits comfortably in a few megabytes of memory.
Measured on SQLite, each of those round trips costs about half a millisecond,
which is where three quarters of the sync's wall clock went.

So the whole roster for the owner and client is read in three queries at the
start of a run and held in dictionaries. Resolution becomes a dict lookup and
the write path is the only thing left that touches the database.

The obvious objection is memory, and it is worth answering with a number rather
than a shrug. Each entry holds an integer primary key, two short strings and a
digest: on the order of 300 bytes. A forty thousand member plan is therefore
about twelve megabytes, which is less than the Django process costs to start.
A plan large enough for that to matter is a plan that should be on Postgres with
a different strategy entirely, and load() takes a `limit` for exactly that case:
above it, the index reports itself unusable and the callers fall back to the
per-loop queries they used before, which are slow but correct.

The index is a read-through cache with explicit writes, not a magic one. Nothing
in it updates itself. The sync calls remember() after it creates a person, so a
subscriber created in pass one is resolvable by pass two without a query, and a
member appearing twice in the same file resolves to the row the first
appearance created rather than colliding on a unique constraint.
"""

from __future__ import annotations

import logging

from members.models import Dependant, Member, Subscriber

logger = logging.getLogger("edi.roster_index")

# Above this many people in one client, the index is not built and the sync
# falls back to per-loop queries. See the module docstring.
MAX_INDEXED_MEMBERS = 250_000


class RosterIndex:
    """
    Identity lookups for one owner/client pair, answered from memory.

    Three maps over Member, keyed by the three things an 834 can identify a
    person with, plus two maps over the projected master tables so roster_sync
    does not repeat the work.
    """

    __slots__ = (
        "owner_id",
        "client_id",
        "usable",
        "by_member_id",
        "by_fingerprint",
        "by_family",
        "digests",
        "subscriber_by_member",
        "dependant_by_member",
        "subscriber_by_ssn",
        "dependant_by_ssn",
        "hits",
        "misses",
    )

    def __init__(self, owner_id, client_id):
        self.owner_id = owner_id
        self.client_id = client_id
        self.usable = False
        # member_id -> Member pk
        self.by_member_id = {}
        # (member_type, ssn_fingerprint) -> Member pk
        self.by_fingerprint = {}
        # (subscriber pk, relationship, dob iso, gender) -> Member pk, for the
        # dependants a sponsor gives no identifier of their own.
        self.by_family = {}
        # Member pk -> content digest of the loop that last wrote it
        self.digests = {}
        # Member pk -> Subscriber / Dependant pk
        self.subscriber_by_member = {}
        self.dependant_by_member = {}
        # ssn (normalised) -> Subscriber / Dependant pk
        self.subscriber_by_ssn = {}
        self.dependant_by_ssn = {}
        self.hits = 0
        self.misses = 0

    # -- construction -------------------------------------------------

    @classmethod
    def load(cls, owner, client, limit: int = MAX_INDEXED_MEMBERS) -> "RosterIndex":
        """
        Read the roster in three queries. Returns an unusable index if the
        client is too large to hold, which every caller treats as "do it the
        old way" rather than as an error.
        """
        index = cls(getattr(owner, "pk", owner), getattr(client, "pk", client))

        members = Member.objects.filter(owner=owner)
        if client is not None:
            members = members.filter(client=client)

        count = members.count()
        if count > limit:
            logger.warning(
                "Roster for client %s has %s members, above the %s index limit; "
                "falling back to per-loop identity queries.",
                index.client_id,
                count,
                limit,
            )
            return index

        rows = members.values_list(
            "pk",
            "member_id",
            "member_type",
            "ssn_fingerprint",
            "subscriber_id",
            "relationship_code",
            "date_of_birth",
            "gender_code",
            "content_digest",
        )
        for (
            pk,
            member_id,
            member_type,
            fingerprint,
            subscriber_id,
            relationship,
            dob,
            gender,
            content_digest,
        ) in rows.iterator(chunk_size=5000):
            if member_id:
                index.by_member_id[member_id] = pk
            if fingerprint:
                index.by_fingerprint.setdefault((member_type, fingerprint), pk)
            if subscriber_id and member_type == "DEP":
                index.by_family.setdefault(
                    _family_key(subscriber_id, relationship, dob, gender), pk
                )
            if content_digest:
                index.digests[pk] = content_digest

        subscribers = Subscriber.objects.filter(owner=owner)
        dependants = Dependant.objects.filter(owner=owner)
        if client is not None:
            subscribers = subscribers.filter(client=client)
            dependants = dependants.filter(client=client)

        for pk, source_member_id, ssn in subscribers.values_list(
            "pk", "source_member_id", "ssn"
        ).iterator(chunk_size=5000):
            if source_member_id:
                index.subscriber_by_member[source_member_id] = pk
            if ssn:
                index.subscriber_by_ssn.setdefault(ssn, pk)

        for pk, source_member_id, ssn in dependants.values_list(
            "pk", "source_member_id", "ssn"
        ).iterator(chunk_size=5000):
            if source_member_id:
                index.dependant_by_member[source_member_id] = pk
            if ssn:
                index.dependant_by_ssn.setdefault(ssn, pk)

        index.usable = True
        logger.info(
            "Roster index built for client %s: %s members, %s subscribers, %s dependants.",
            index.client_id,
            count,
            len(index.subscriber_by_member),
            len(index.dependant_by_member),
        )
        return index

    # -- lookups ------------------------------------------------------

    def find_member(self, parsed_dict: dict, current_subscriber_pk=None):
        """
        The primary key of the member this loop describes, or None.

        Same order of preference as the query-based resolver it replaces:
        sponsor member id, then family position for an unidentified dependant,
        then SSN fingerprint. Returning a pk rather than a Member is deliberate;
        the caller fetches the row only on the slow path, and the fast path
        never needs it at all.
        """
        member_id = (parsed_dict.get("member_id") or "").strip()
        if member_id:
            found = self.by_member_id.get(member_id)
            if found is not None:
                self.hits += 1
                return found

        member_type = parsed_dict.get("member_type") or "SUB"

        if member_type == "DEP" and current_subscriber_pk:
            key = _family_key(
                current_subscriber_pk,
                parsed_dict.get("relationship_code"),
                parsed_dict.get("date_of_birth"),
                parsed_dict.get("gender_code"),
            )
            found = self.by_family.get(key)
            if found is not None:
                self.hits += 1
                return found

        fingerprint = parsed_dict.get("ssn_fingerprint")
        if fingerprint:
            found = self.by_fingerprint.get((member_type, fingerprint))
            if found is not None:
                self.hits += 1
                return found

        self.misses += 1
        return None

    def digest_of(self, member_pk):
        return self.digests.get(member_pk)

    def roster_pk(self, member_pk, member_type):
        if member_type == "SUB":
            return self.subscriber_by_member.get(member_pk)
        return self.dependant_by_member.get(member_pk)

    # -- writes -------------------------------------------------------

    def remember(self, member, digest=None):
        """
        Record a member the current run created or changed.

        Called after every slow-path write so the rest of the file — the second
        pass, a repeated appearance, a dependant that arrives later — resolves
        against what this run has already done instead of against a stale
        snapshot taken before it started.
        """
        pk = member.pk
        if member.member_id:
            self.by_member_id[member.member_id] = pk
        if member.ssn_fingerprint:
            self.by_fingerprint[(member.member_type, member.ssn_fingerprint)] = pk
        if member.member_type == "DEP" and member.subscriber_id:
            self.by_family[
                _family_key(
                    member.subscriber_id,
                    member.relationship_code,
                    member.date_of_birth,
                    member.gender_code,
                )
            ] = pk
        if digest is not None:
            self.digests[pk] = digest

    def remember_roster(self, member_pk, member_type, roster_record):
        if roster_record is None:
            return
        if member_type == "SUB":
            self.subscriber_by_member[member_pk] = roster_record.pk
            if roster_record.ssn:
                self.subscriber_by_ssn[roster_record.ssn] = roster_record.pk
        else:
            self.dependant_by_member[member_pk] = roster_record.pk
            if roster_record.ssn:
                self.dependant_by_ssn[roster_record.ssn] = roster_record.pk

    def forget_digest(self, member_pk):
        """Drop a digest so the next appearance takes the slow path."""
        self.digests.pop(member_pk, None)


def _family_key(subscriber_pk, relationship, dob, gender):
    return (
        int(subscriber_pk),
        (relationship or "").strip(),
        dob.isoformat() if hasattr(dob, "isoformat") else (dob or ""),
        (gender or "").strip(),
    )
