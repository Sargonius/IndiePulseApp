from __future__ import annotations

import praw
from typing import Tuple
from loguru import logger


class RedditClient:
    def __init__(self, client_id: str, client_secret: str, username: str, password: str):
        self.reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            user_agent="IndiePulse/1.0"
        )

    def submit_text(self, subreddit_name: str, title: str, text: str) -> Tuple[str, str]:
        """
        Submit a text post to a subreddit.
        Returns (submission_id, submission_url)
        """
        try:
            subreddit = self.reddit.subreddit(subreddit_name)
            submission = subreddit.submit(
                title=title,
                selftext=text
            )
            
            return submission.id, submission.url
            
        except Exception as e:
            logger.error(f"Reddit submission failed: {e}")
            raise RuntimeError(f"Reddit submission failed: {e}")

    def test_connection(self) -> bool:
        """Test if the Reddit connection is working"""
        try:
            # Try to access user info
            user = self.reddit.user.me()
            return user is not None
        except Exception as e:
            logger.error(f"Reddit connection test failed: {e}")
            return False


