from __future__ import annotations

import time
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
            user_agent="IndiePulse/1.0",
            # Don't nag about "newer" releases — 8.x dropped submit_image/submit_video,
            # so the upgrade is intentionally held back (see requirements.txt pin).
            check_for_updates=False,
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

    @staticmethod
    def _is_websocket_error(exc: Exception) -> bool:
        """PRAW raises this when it can't open the websocket it uses to wait for the
        finished submission — but the image/video post is created on Reddit anyway."""
        return 'websocket' in str(exc).lower()

    def _find_recent_submission(self, subreddit_name: str, title: str,
                                attempts: int = 6, delay: float = 2.5):
        """Recover a just-created post after a websocket failure by polling the
        authenticated user's newest submissions and matching title + subreddit."""
        target = (title or '').strip()
        sub_lc = (subreddit_name or '').strip().lower()
        started = time.time()
        for _ in range(attempts):
            try:
                me = self.reddit.user.me()
                for subm in me.submissions.new(limit=10):
                    try:
                        # Only posts created around now, to skip older same-title posts
                        if (started - float(getattr(subm, 'created_utc', 0))) > 900:
                            continue
                        if (subm.title or '').strip() != target:
                            continue
                        if sub_lc and str(subm.subreddit).lower() != sub_lc:
                            continue
                        return subm
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(delay)
        return None

    def submit_image(self, subreddit_name: str, title: str, image_path: str) -> Tuple[str, str]:
        subreddit = self.reddit.subreddit(subreddit_name)
        try:
            submission = subreddit.submit_image(title=title, image_path=image_path)
            return submission.id, submission.url
        except Exception as e:
            # The post is usually live despite the websocket error — recover it.
            if self._is_websocket_error(e):
                logger.warning(f"Reddit image submit websocket failed; recovering by polling: {e}")
                recovered = self._find_recent_submission(subreddit_name, title)
                if recovered is not None:
                    logger.info(f"Reddit image submit recovered id={recovered.id}")
                    return recovered.id, recovered.url
                logger.error("Reddit image submit: websocket failed and recovery found no post")
            logger.error(f"Reddit image submission failed: {e}")
            raise RuntimeError(f"Reddit image submission failed: {e}")

    def submit_video(self, subreddit_name: str, title: str, video_path: str, thumbnail_path: str | None = None) -> Tuple[str, str]:
        subreddit = self.reddit.subreddit(subreddit_name)
        try:
            submission = subreddit.submit_video(title=title, video_path=video_path, thumbnail_path=thumbnail_path)
            return submission.id, submission.url
        except Exception as e:
            # Videos are even more prone to the websocket error; the post is usually live.
            if self._is_websocket_error(e):
                logger.warning(f"Reddit video submit websocket failed; recovering by polling: {e}")
                recovered = self._find_recent_submission(subreddit_name, title)
                if recovered is not None:
                    logger.info(f"Reddit video submit recovered id={recovered.id}")
                    return recovered.id, recovered.url
                logger.error("Reddit video submit: websocket failed and recovery found no post")
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


