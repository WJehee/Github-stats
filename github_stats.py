#!/usr/bin/python3

import asyncio
import os
from typing import Dict, List, Optional, Set, Tuple, Any, cast

import aiohttp
import requests


###############################################################################
# Main Classes
###############################################################################


class Queries(object):
    """
    Class with functions to query the GitHub GraphQL (v4) API and the REST (v3)
    API. Also includes functions to dynamically generate GraphQL queries.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        max_connections: int = 10,
    ):
        self.username = username
        self.access_token = access_token
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)

    async def query(self, generated_query: str) -> Dict:
        """
        Make a request to the GraphQL API using the authentication token from
        the environment
        :param generated_query: string query to be sent to the API
        :return: decoded GraphQL JSON output
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }
        try:
            async with self.semaphore:
                r_async = await self.session.post(
                    "https://api.github.com/graphql",
                    headers=headers,
                    json={"query": generated_query},
                )
            result = await r_async.json()
            if result is not None:
                return result
        except:
            print("aiohttp failed for GraphQL query")
            # Fall back on non-async requests
            async with self.semaphore:
                r_requests = requests.post(
                    "https://api.github.com/graphql",
                    headers=headers,
                    json={"query": generated_query},
                )
                result = r_requests.json()
                if result is not None:
                    return result
        return dict()

    async def query_rest(self, path: str, params: Optional[Dict] = None) -> Dict:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :return: deserialized REST JSON output
        """

        for _ in range(60):
            headers = {
                "Authorization": f"token {self.access_token}",
            }
            if params is None:
                params = dict()
            if path.startswith("/"):
                path = path[1:]
            try:
                async with self.semaphore:
                    r_async = await self.session.get(
                        f"https://api.github.com/{path}",
                        headers=headers,
                        params=tuple(params.items()),
                    )
                if r_async.status == 202:
                    print(f"A path returned 202. Retrying...")
                    await asyncio.sleep(2)
                    continue

                result = await r_async.json()
                if result is not None:
                    return result
            except:
                print("aiohttp failed for rest query")
                # Fall back on non-async requests
                async with self.semaphore:
                    r_requests = requests.get(
                        f"https://api.github.com/{path}",
                        headers=headers,
                        params=tuple(params.items()),
                    )
                    if r_requests.status_code == 202:
                        print(f"A path returned 202. Retrying...")
                        await asyncio.sleep(2)
                        continue
                    elif r_requests.status_code == 200:
                        return r_requests.json()
        print("There were too many 202s. Data for this repository will be incomplete.")
        return dict()

    @staticmethod
    def repos_overview(
        contrib_cursor: Optional[str] = None, owned_cursor: Optional[str] = None
    ) -> str:
        """
        :return: GraphQL query with overview of user repositories
        """
        return f"""{{
  viewer {{
    login,
    name,
    pullRequests(first: 1) {{
        totalCount
    }}
    openIssues: issues(states: OPEN) {{
        totalCount
    }}
    closedIssues: issues(states: CLOSED) {{
        totalCount
    }}
    repositories(
        first: 100,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        isFork: false,
        after: {"null" if owned_cursor is None else '"'+ owned_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
    repositoriesContributedTo(
        first: 100,
        includeUserRepositories: false,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        contributionTypes: [
            COMMIT,
            PULL_REQUEST,
            REPOSITORY,
            PULL_REQUEST_REVIEW
        ]
        after: {"null" if contrib_cursor is None else '"'+ contrib_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

    @staticmethod
    def contrib_years() -> str:
        """
        :return: GraphQL query to get all years the user has been a contributor
        """
        return """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""

    @staticmethod
    def contribs_by_year(year: str) -> str:
        """
        :param year: year to query for
        :return: portion of a GraphQL query with desired info for a given year
        """
        return f"""
    year{year}: contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{int(year) + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}
"""

    @classmethod
    def all_contribs(cls, years: List[str]) -> str:
        """
        :param years: list of years to get contributions for
        :return: query to retrieve contribution information for all user years
        """
        by_years = "\n".join(map(cls.contribs_by_year, years))
        return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""


class Stats(object):
    """
    Retrieve and store statistics about GitHub usage.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        exclude_repos: Optional[Set] = None,
        exclude_langs: Optional[Set] = None,
        ignore_forked_repos: bool = False,
    ):
        self.username = username
        self._ignore_forked_repos = ignore_forked_repos
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self.queries = Queries(username, access_token, session)

        self._name: Optional[str] = None
        self._total_contributions: Optional[int] = None
        self._languages: Optional[Dict[str, Any]] = None
        self._repos: Optional[Set[str]] = None
        self._issues: int = 0
        self._prs: int = 0

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        return f"""Name: {await self.name}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.repos)}
Languages:
  - {formatted_languages}"""

    async def get_stats(self) -> None:
        """
        Get lots of summary statistics using one big query. Sets many attributes
        """
        self._languages = dict()
        self._repos = set()

        exclude_langs_lower = {x.lower() for x in self._exclude_langs}

        next_owned = None
        next_contrib = None
        while True:
            raw_results = await self.queries.query(
                Queries.repos_overview(
                    owned_cursor=next_owned, contrib_cursor=next_contrib
                )
            )
            raw_results = raw_results if raw_results is not None else {}

            viewer = raw_results.get("data", {}).get("viewer", {})
            self._name = viewer.get("name", None)
            if self._name is None:
                self._name = viewer.get("login", "No Name")

            contrib_repos = viewer.get("repositoriesContributedTo", {})
            owned_repos = viewer.get("repositories", {})
            issuesO = viewer.get("openIssues", {}).get("totalCount", 0)
            issuesC = viewer.get("closedIssues", {}).get("totalCount", 0)
            self._issues = issuesC + issuesO
            self._prs = viewer.get("pullRequests", {}).get("totalCount", 0)

            repos = owned_repos.get("nodes", [])
            if not self._ignore_forked_repos:
                repos += contrib_repos.get("nodes", [])

            for repo in repos:
                if repo is None:
                    continue
                name = repo.get("nameWithOwner")
                if name in self._repos or name in self._exclude_repos:
                    continue
                self._repos.add(name)

                for lang in repo.get("languages", {}).get("edges", []):
                    name = lang.get("node", {}).get("name", "Other")
                    languages = await self.languages
                    if name.lower() in exclude_langs_lower:
                        continue
                    if name in languages:
                        languages[name]["size"] += lang.get("size", 0)
                        languages[name]["occurrences"] += 1
                    else:
                        languages[name] = {
                            "size": lang.get("size", 0),
                            "occurrences": 1,
                            "color": lang.get("node", {}).get("color"),
                        }

            if owned_repos.get("pageInfo", {}).get(
                "hasNextPage", False
            ) or contrib_repos.get("pageInfo", {}).get("hasNextPage", False):
                next_owned = owned_repos.get("pageInfo", {}).get(
                    "endCursor", next_owned
                )
                next_contrib = contrib_repos.get("pageInfo", {}).get(
                    "endCursor", next_contrib
                )
            else:
                break

        # TODO: Improve languages to scale by number of contributions to
        #       specific filetypes
        langs_total = sum([v.get("size", 0) for v in self._languages.values()])
        for k, v in self._languages.items():
            v["prop"] = 100 * (v.get("size", 0) / langs_total)

    @property
    async def name(self) -> str:
        """
        :return: GitHub user's name (e.g., Jacob Strieb)
        """
        if self._name is not None:
            return self._name
        await self.get_stats()
        assert self._name is not None
        return self._name

    @property
    async def languages(self) -> Dict:
        """
        :return: summary of languages used by the user
        """
        if self._languages is not None:
            return self._languages
        await self.get_stats()
        assert self._languages is not None
        return self._languages

    @property
    async def languages_proportional(self) -> Dict:
        """
        :return: summary of languages used by the user, with proportional usage
        """
        if self._languages is None:
            await self.get_stats()
            assert self._languages is not None

        return {k: v.get("prop", 0) for (k, v) in self._languages.items()}

    @property
    async def repos(self) -> Set[str]:
        """
        :return: list of names of user's repos
        """
        if self._repos is not None:
            return self._repos
        await self.get_stats()
        assert self._repos is not None
        return self._repos

    @property
    async def issues(self) -> int:
        """
        :return: number of issues created by the user
        """
        if self._issues is None:
            await self.get_stats()
        return self._issues

    @property
    async def prs(self) -> int:
        """
        :return: number of prs created by the user
        """
        if self._prs is None:
            await self.get_stats()
        return self._prs

    @property
    async def total_contributions(self) -> int:
        """
        :return: count of user's total contributions as defined by GitHub
        """
        if self._total_contributions is not None:
            return self._total_contributions

        self._total_contributions = 0
        years = (
            (await self.queries.query(Queries.contrib_years()))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
            .get("contributionYears", [])
        )
        by_year = (
            (await self.queries.query(Queries.all_contribs(years)))
            .get("data", {})
            .get("viewer", {})
            .values()
        )
        for year in by_year:
            self._total_contributions += year.get("contributionCalendar", {}).get(
                "totalContributions", 0
            )
        return cast(int, self._total_contributions)

###############################################################################
# Main Function
###############################################################################


async def main() -> None:
    """
    Used mostly for testing; this module is not usually run standalone
    """
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    if access_token is None or user is None:
        raise RuntimeError(
            "ACCESS_TOKEN and GITHUB_ACTOR environment variables cannot be None!"
        )
    async with aiohttp.ClientSession() as session:
        s = Stats(user, access_token, session)
        print(await s.to_str())


if __name__ == "__main__":
    asyncio.run(main())
