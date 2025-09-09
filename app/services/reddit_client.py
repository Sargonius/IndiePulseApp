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

    def submit_image(self, subreddit_name: str, title: str, image_path: str) -> Tuple[str, str]:
        try:
            subreddit = self.reddit.subreddit(subreddit_name)
            submission = subreddit.submit_image(title=title, image_path=image_path)
            return submission.id, submission.url
        except Exception as e:
            logger.error(f"Reddit image submission failed: {e}")
            raise RuntimeError(f"Reddit image submission failed: {e}")

    def submit_video(self, subreddit_name: str, title: str, video_path: str, thumbnail_path: str | None = None) -> Tuple[str, str]:
        try:
            subreddit = self.reddit.subreddit(subreddit_name)
            submission = subreddit.submit_video(title=title, video_path=video_path, thumbnail_path=thumbnail_path)
            return submission.id, submission.url
        except Exception as e:
            logger.error(f"Reddit video submission failed: {e}")
            raise RuntimeError(f"Reddit video submission failed: {e}")

    def submit_link(self, subreddit_name: str, title: str, url: str) -> Tuple[str, str]:
        try:
            subreddit = self.reddit.subreddit(subreddit_name)
            submission = subreddit.submit(title=title, url=url)
            return submission.id, submission.url
        except Exception as e:
            logger.error(f"Reddit link submission failed: {e}")
            raise RuntimeError(f"Reddit link submission failed: {e}")

    def comment(self, submission_id: str, text: str) -> None:
        try:
            subm = self.reddit.submission(id=submission_id)
            subm.reply(text)
        except Exception as e:
            logger.error(f"Reddit comment failed: {e}")

    def test_connection(self) -> bool:
        """Test if the Reddit connection is working"""
        try:
            # Try to access user info
            user = self.reddit.user.me()
            return user is not None
        except Exception as e:
            logger.error(f"Reddit connection test failed: {e}")
            return False


