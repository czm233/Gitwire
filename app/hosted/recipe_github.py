"""Run legacy public recipes through the hosted read-only visibility boundary."""
import re
import httpx
from app.github import GithubClient
from app.hosted import github


class PublicRecipeTransport(httpx.AsyncBaseTransport):
    def __init__(self, runtime):
        self.runtime = runtime
        self.deferred = None

    async def handle_async_request(self, request):
        url = request.url
        if request.method != 'GET' or url.scheme != 'https' or url.host != 'api.github.com':
            return httpx.Response(405, json={'message': 'Read-only GitHub access'})
        match = re.fullmatch(r'/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(/.*)?', url.path)
        if not match:
            return httpx.Response(404, json={'message': 'Public repository API required'})
        try:
            data = await github.get(self.runtime, url.path, dict(url.params), cached=True) if match.group(2) else await github.public_repo(self.runtime, match.group(1))
            return httpx.Response(200, json=data)
        except github.GitHubFailure as exc:
            if exc.status == 429 or exc.status >= 500:
                self.deferred = exc
            return httpx.Response(exc.status, json={'message': exc.safe_message})


class PublicRecipeGithub(GithubClient):
    def __init__(self, runtime):
        self.boundary = PublicRecipeTransport(runtime)
        super().__init__(transport=self.boundary)
