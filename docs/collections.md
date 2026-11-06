# Workshop collection resolution

## Investigation and choice

The resolver already attempted to expand collections, but only when
`GetPublishedFileDetails` returned `file_type == 2`. A live metadata query on
2026-11-06 for public collection `496643861` returned `result: 1`, omitted
`file_type`, and supplied an image URL as `file_url`. The previous resolver
therefore queued the cover as an ordinary file. Existing tests supplied
`file_type` explicitly and missed this response shape.

Alternatives considered:

| Approach | Benefits | Costs |
| --- | --- | --- |
| Parse Workshop pages with BeautifulSoup | No API key; matches the approach in SCMD Workshop Downloader 2 | Adds a dependency and HTML selectors; must distinguish collection members from unrelated links; depends on page markup |
| Use `GetCollectionDetails` | Official public API; structured child IDs; batches collections; reuses existing HTTP retries/timeouts | Needs an extra batched query when metadata omits the item type |
| Authenticated Workshop API or Steam client integration | Richer metadata and authenticated access | Adds credential/setup requirements and complexity to a public downloader |

Use `GetCollectionDetails`; no additional dependency, authentication, or scraping
fallback is necessary. The Steamworks reference lists no key parameter for this
method. SteamCMD continues to download individual resolved items using their own
App IDs and the shared queue of persistent sessions.

Sources:

- [Valve: ISteamRemoteStorage Web API](https://partner.steamgames.com/doc/webapi/ISteamRemoteStorage)
- [SCMD List Manager source](https://github.com/BerdyAlexei/SCMD-Workshop-Downloader-2/blob/main/SCMD%20List%20Manager.py): parses page anchors with BeautifulSoup and generates individual `workshop_download_item` commands.

## Implementation

1. Fetch metadata in batches and query collections for successful items whose
   type is a collection or whose `file_type` is absent. A successful collection
   response, including an empty child list, takes precedence over its `file_url`.
2. Discover children level by level, fetching each ID's metadata once. Batch
   collection requests in groups of 100, matching metadata request batching.
3. Flatten the discovered graph iteratively in collection/input order. Visit each
   ID once to handle duplicates, shared descendants, and cycles without recursion.
4. Pass only leaf items and explicit resolution failures to the existing download
   pipeline. A known collection whose members cannot be read remains a failure;
   unavailable child metadata follows existing item error handling.

An ordinary item without `file_type` also incurs a collection lookup, since its
metadata alone cannot safely distinguish a mod URL from a collection cover.
Per-item non-success collection responses are treated as non-collections unless
metadata explicitly identifies a collection. Transport/HTTP errors abort
resolution rather than silently downloading possible covers. This does not grant
access to private/deleted collections or bypass game ownership requirements.

## Verification

Regression tests exercise legacy metadata with a cover URL, nested/shared/cyclic
collections, unavailable children, cancellation between expansion levels, and
collection request batching. Existing tests cover empty/unreadable collections
and the HTTP/SteamCMD download paths. Live verification queried public metadata;
no Workshop content was downloaded during the investigation.
