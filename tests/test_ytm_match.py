from tapedeck.models import Track
from tapedeck.ytm import YTM


class FakeYTM(YTM):
    def __init__(self, pages):
        self.pages, self.queries = pages, []

    def search(self, query, limit=10):
        self.queries.append(query)
        return self.pages.pop(0) if self.pages else []


def test_match_retries_with_clean_query_when_first_search_misses():
    want = Track("Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
                 ["Daft Punk, Pharrell Williams, Nile Rodgers"], 369, spotify_id="sp")
    right = Track("Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
                  ["Daft Punk", "Pharrell Williams", "Nile Rodgers"], 370, video_id="v")
    y = FakeYTM([[Track("Get Lucky", ["Petrol Game"], 242, video_id="x")], [right]])
    hit = y.match(want)
    assert hit is right and hit.spotify_id == "sp"
    assert y.queries[1] == "Daft Punk get lucky"
