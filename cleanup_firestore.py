#!/usr/bin/env python3
"""
cleanup_firestore.py — Opschonen van verouderde data in de drie Firestore-
collecties die door de regulatory reporting agents worden gebruikt.

Wat wordt opgeschoond (met standaard-drempels, aan te passen via CLI-flags):

  vendor_impact_items    status == "closed" AND last_updated ouder dan 180 dagen
  last_mile_tracked_items  last_seen ouder dan 90 dagen (item duikt kennelijk
                           niet meer op in scans — waarschijnlijk afgehandeld
                           of niet langer relevant)
  report_feedback        addressed == True AND submitted_at ouder dan 30 dagen

Gebruik:
  python3 cleanup_firestore.py                # dry-run, toont wat verwijderd zou worden
  python3 cleanup_firestore.py --apply         # voert de verwijdering echt uit
  python3 cleanup_firestore.py --apply --yes   # zonder interactieve bevestiging (voor cron/scheduler)

  # drempels aanpassen:
  python3 cleanup_firestore.py --apply \
      --vendor-closed-days 90 \
      --stale-tracked-days 60 \
      --feedback-days 14

Vereist: google-cloud-firestore (staat al in requirements.txt van de agent)
         en geldige gcloud/ADC-credentials met datastore.user rol.
"""

import argparse
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

PROJECT_ID = "project-5ce9338e-bc20-4b9f-88f"

VENDOR_COLLECTION = "vendor_impact_items"
TRACKED_COLLECTION = "last_mile_tracked_items"
FEEDBACK_COLLECTION = "report_feedback"


def parse_iso(value):
    """Best-effort parse of the ISO timestamps stored by the agents."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def find_stale_vendor_items(db, closed_before):
    stale = []
    for doc in db.collection(VENDOR_COLLECTION).where("status", "==", "closed").stream():
        data = doc.to_dict()
        last_updated = parse_iso(data.get("last_updated"))
        if last_updated and last_updated < closed_before:
            stale.append((doc.id, data.get("title", "(zonder titel)"), last_updated))
    return stale


def find_stale_tracked_items(db, seen_before):
    stale = []
    for doc in db.collection(TRACKED_COLLECTION).stream():
        data = doc.to_dict()
        last_seen = parse_iso(data.get("last_seen"))
        if last_seen and last_seen < seen_before:
            stale.append((doc.id, data.get("topic", "(zonder topic)"), last_seen))
    return stale


def find_stale_feedback(db, submitted_before):
    stale = []
    for doc in db.collection(FEEDBACK_COLLECTION).where("addressed", "==", True).stream():
        data = doc.to_dict()
        submitted_at = parse_iso(data.get("submitted_at"))
        if submitted_at and submitted_at < submitted_before:
            stale.append((doc.id, data.get("comment", "")[:60], submitted_at))
    return stale


def print_section(title, items):
    print(f"\n{title} ({len(items)} item(s))")
    print("-" * len(title))
    for doc_id, label, when in items:
        print(f"  [{doc_id}] {label}  (laatst bijgewerkt: {when.date()})")
    if not items:
        print("  (niets te verwijderen)")


def delete_docs(db, collection, doc_ids):
    batch = db.batch()
    count = 0
    for doc_id in doc_ids:
        batch.delete(db.collection(collection).document(doc_id))
        count += 1
        if count % 400 == 0:  # Firestore batch limit is 500
            batch.commit()
            batch = db.batch()
    if count % 400 != 0:
        batch.commit()
    return count


def main():
    parser = argparse.ArgumentParser(description="Ruim verouderde Firestore-data op.")
    parser.add_argument("--apply", action="store_true",
                         help="Voer de verwijdering echt uit (standaard: dry-run).")
    parser.add_argument("--yes", action="store_true",
                         help="Sla de interactieve bevestiging over (handig voor scheduled runs).")
    parser.add_argument("--vendor-closed-days", type=int, default=180,
                         help="Verwijder 'closed' vendor-items ouder dan N dagen (default 180).")
    parser.add_argument("--stale-tracked-days", type=int, default=90,
                         help="Verwijder tracked items die N dagen niet meer zijn gezien (default 90).")
    parser.add_argument("--feedback-days", type=int, default=30,
                         help="Verwijder afgehandelde feedback ouder dan N dagen (default 30).")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    vendor_cutoff = now - timedelta(days=args.vendor_closed_days)
    tracked_cutoff = now - timedelta(days=args.stale_tracked_days)
    feedback_cutoff = now - timedelta(days=args.feedback_days)

    db = firestore.Client(project=PROJECT_ID)

    stale_vendor = find_stale_vendor_items(db, vendor_cutoff)
    stale_tracked = find_stale_tracked_items(db, tracked_cutoff)
    stale_feedback = find_stale_feedback(db, feedback_cutoff)

    print("=" * 70)
    print(f"Firestore cleanup — project {PROJECT_ID}")
    print(f"Modus: {'APPLY (echt verwijderen)' if args.apply else 'DRY-RUN (alleen tonen)'}")
    print("=" * 70)

    print_section(f"vendor_impact_items — closed, ouder dan {args.vendor_closed_days} dagen", stale_vendor)
    print_section(f"last_mile_tracked_items — niet gezien in {args.stale_tracked_days} dagen", stale_tracked)
    print_section(f"report_feedback — afgehandeld, ouder dan {args.feedback_days} dagen", stale_feedback)

    total = len(stale_vendor) + len(stale_tracked) + len(stale_feedback)
    print(f"\nTotaal te verwijderen: {total}")

    if not args.apply:
        print("\nDit was een dry-run — er is niets verwijderd.")
        print("Draai met --apply om de verwijdering echt uit te voeren.")
        return

    if total == 0:
        print("\nNiets om op te schonen.")
        return

    if not args.yes:
        confirm = input(f"\n{total} document(en) definitief verwijderen? Type 'ja' om te bevestigen: ")
        if confirm.strip().lower() != "ja":
            print("Geannuleerd.")
            return

    deleted = 0
    deleted += delete_docs(db, VENDOR_COLLECTION, [d[0] for d in stale_vendor])
    deleted += delete_docs(db, TRACKED_COLLECTION, [d[0] for d in stale_tracked])
    deleted += delete_docs(db, FEEDBACK_COLLECTION, [d[0] for d in stale_feedback])

    print(f"\n{deleted} document(en) verwijderd.")


if __name__ == "__main__":
    main()
