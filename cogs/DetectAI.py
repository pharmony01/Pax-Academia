import asyncio
import colorsys
import os
from dataclasses import dataclass
from enum import Enum
from typing import Literal

import bs4
import discord
import undetected_chromedriver as uc
from discord import option
from discord.commands.context import ApplicationContext
from discord.ext import commands
from selenium.common.exceptions import TimeoutException as SeleniumTimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from util.EmbedBuilder import EmbedBuilder

DETECT_AI_GUILD = os.getenv("ALLOW_DETECT_AI_GUILD")
DETECT_AI_ROLE = os.getenv("ALLOW_DETECT_AI_ROLE")
DETECT_AI_PERMISSIONS = (
    (lambda x: x) if DETECT_AI_ROLE is None else commands.has_role(DETECT_AI_ROLE)
)


class AuthorPredication(Enum):
    Human = "Written by a __**Human**__"
    ArtificialIntelligence = "Generated by __**AI**__"


AuthorPredicationValue = (
    Literal[AuthorPredication.Human] | Literal[AuthorPredication.ArtificialIntelligence]
)


@dataclass
class AIDetectionResult:
    author_predication: AuthorPredicationValue
    confidence: float  # 0 - 1
    word_count: int
    text: str
    parts: list["AIDetectionResult"]

    def color_classification(self) -> int:
        """
        Determines what color should be used to classify this result.
        100% AI is bright red and 100% human is bright green.
        :return: a hexadecimal color code
        """

        # the "factor" goes from 0 (AI) to 1 (human)
        if self.author_predication == AuthorPredication.ArtificialIntelligence:
            factor = 0.5 - self.confidence / 2
        else:
            factor = 0.5 + self.confidence / 2

        r, g, b = colorsys.hsv_to_rgb(factor / 3, 0.85, 1)
        return int(r * 255) << 16 | int(g * 255) << 8 | int(b * 255)

    def text_summary(self) -> str:
        """
        Simply grabs the first 5 and the last 5 words, then joins them with a `[...]`.
        Or, just the entire text if it contains less than 11 words.
        Includes the word count.
        :return: str
        """

        words = self.text.split()

        if len(words) <= 10:
            text_snippet = self.text
        else:
            text_snippet = " ".join(words[:5]) + " [...] " + " ".join(words[-5:])

        return f"{self.word_count} words\n{text_snippet}"

    def __str__(self) -> str:
        """
        Formats the result for human viewing. Generates strings like:
        * Inconclusive
        * Plausibly Written by a __**Human**__
          42.3% Confident
        * Probably Generated by __**AI**__
          87.9% Confident
        * Certainly Generated by __**AI**__
          99.2% Confident
        :return: str
        """
        if self.confidence < 0.2:
            return "Inconclusive"
        if self.confidence < 0.6:
            return f"Plausibly {self.author_predication.value}\n{self.confidence:.1%} Confident"
        if self.confidence < 0.9:
            return f"Probably {self.author_predication.value}\n{self.confidence:.1%} Confident"

        return f"Certainly {self.author_predication.value}\n{self.confidence:.1%} Confident"


def launch_chrome() -> uc.Chrome:
    """
    Opens an undetected (and headless) chrome driver.
    :return: uc.Chrome
    """
    options = uc.ChromeOptions()
    options.add_argument("--headless")
    return uc.Chrome(options=options)


def submit_text(driver: uc.Chrome, text: str) -> None:
    """
    1. finds the text field
    2. enters the provided text
    3. finds the submit button
    4. clicks the submit button
    """
    driver.find_element(By.CSS_SELECTOR, "textarea").send_keys(text)
    driver.find_element(By.CSS_SELECTOR, "button").click()


def wait_for_rate_limit_error(driver: uc.Chrome, timeout: float) -> bool:
    """
    Waits for the rate limit error to show up.
    :return: True if it was found, and false otherwise.
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "/html/body/app-root/div/app-scan-inline-widget-layout/app-error-page/div/div/span/div/b",
                ),
            ),
        )
        return True  # found it!
    except SeleniumTimeoutException:
        return False  # timed out... did not find it


def wait_for_scan_result(driver: uc.Chrome, timeout: float) -> bool:
    """
    Waits for the scan result to show up.
    :return: True if it was found, and false otherwise.
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div.scan-text-editor-result"),
            ),
        )
        return True  # found it!
    except SeleniumTimeoutException:
        return False  # timed out... did not find it


def wait_for_processing_completion(driver: uc.Chrome) -> bool:
    """
    Waits for the copyleaks page to finish processing the text, or to throw a rate limit error.
    Will wait a maximum of 20 seconds before raising a TimeoutError.
    :return: True when we were rate limited, False otherwise.
    """

    for _ in range(10):
        if wait_for_rate_limit_error(driver, 1):
            return True

        if wait_for_scan_result(driver, 1):
            return False

    # did not find it within 20 seconds...
    raise TimeoutError("Failed to complete within 20 seconds.")


def parse_result_element(result_element: bs4.Tag) -> AIDetectionResult:
    """
    Parses the copyleaks scan result into a pythonic AIDetectionResult.

    :param result_element: The <div class="scan-text-editor-result"> element from the copyleaks page.
    :type result_element: bs4.Tag
    :return: the parsed AIDetectionResult
    """

    parts: list[AIDetectionResult] = []
    for span_element in result_element.select("span"):
        try:
            scan_words = int(span_element["cl-scan-words"])  # type: ignore - errors caught at runtime!
        except (KeyError, ValueError, TypeError):
            continue

        try:
            scan_probability = float(span_element["cl-scan-probability"])  # type: ignore - errors caught at runtime!
        except (KeyError, ValueError, TypeError):
            continue

        author_predication = (
            AuthorPredication.Human
            if span_element.has_attr("cl-human-match")
            else AuthorPredication.ArtificialIntelligence
        )

        parts.append(
            AIDetectionResult(
                word_count=scan_words,
                text=span_element.text.strip(),
                confidence=scan_probability,
                author_predication=author_predication,
                parts=[],  # individual parts are not broken down further
            ),
        )

    # The copyleaks page _only_ provides the part-by-part breakdown of the overall text.
    # We need to manually calculate the overall result by doing a weighted average of each part.
    # The summation formula can be written as
    # ```latex
    # \frac{\sum_{i=1}^{N} w_i c_i a_i}{\sum_{i=1}^{N} w_i}
    # ```
    # where `w_i` is the number of words in the i'th part
    # and `c_i` is the confidence of the i'th part (from 0 to 1)
    # and `a_i` is the author of the part (-1 being AI and 1 being Human)
    # and `N` is the number of parts
    #
    # The sign of the result indicates the author (negative being AI, positive being Human),
    # and the absolute value indicates the confidence.

    total_words = 0
    total_signed_confidence = 0
    for part in parts:
        total_words += part.word_count
        if part.author_predication == AuthorPredication.Human:
            total_signed_confidence += part.confidence * part.word_count
        else:
            total_signed_confidence -= part.confidence * part.word_count

    overall_signed_confidence = total_signed_confidence / total_words
    if overall_signed_confidence < 0:
        overall_author_predication = AuthorPredication.ArtificialIntelligence
        overall_confidence = -overall_signed_confidence
    else:
        overall_author_predication = AuthorPredication.Human
        overall_confidence = overall_signed_confidence

    return AIDetectionResult(
        word_count=total_words,
        text=result_element.text.strip(),
        author_predication=overall_author_predication,
        confidence=overall_confidence,
        parts=parts,
    )


def detect_ai(text: str) -> AIDetectionResult:
    """
    Uses copyleaks to process the text and parses the result.
    See live demo: https://app.copyleaks.com/v1/scan/ai/embedded
    :param text: the source text which may have been partially or fully written by AI or humans.
    :type text: str
    :return: a AIDetectionResult
    """
    driver = launch_chrome()
    driver.get("https://app.copyleaks.com/v1/scan/ai/embedded")
    submit_text(driver, text)
    if wait_for_processing_completion(driver):
        driver.quit()
        raise Exception(
            "You have reached your limit for the day. Please try again tomorrow.",
        )

    soup = bs4.BeautifulSoup(driver.page_source, "html.parser")
    driver.quit()

    result_element = soup.select_one(".scan-text-editor-result")
    assert result_element, "`wait_for_processing_completion` verified that this exists"
    return parse_result_element(result_element)


class AI(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @DETECT_AI_PERMISSIONS
    @commands.slash_command(
        name="detect-ai",
        description="Runs text through an AI detector.",
    )
    @option(
        "text",
        str,
        description="The text to run through the AI.",
        required=True,
    )
    async def ai(self, ctx: ApplicationContext, text: str) -> None:
        """
        Runs a given text through an AI detector and returns the result as an image.

        :param ctx: The context of the command, which includes information about the user, channel, and
        server where the command was invoked
        :type ctx: ApplicationContext
        :param text: The text that will be analyzed by the AI detector
        :type text: str
        :param ephemeral: A boolean parameter that determines whether the response message should only be
        visible to the user who triggered the command (True) or visible to everyone in the channel (False)
        :type ephemeral: bool
        :return: The function is not returning anything, it is using the `await` keyword to send responses
        to the user in the Discord chat.
        """
        if not isinstance(ctx.channel, discord.TextChannel):
            return
        if not ctx.channel.category or not ctx.channel.category.name.lower().endswith(
            "help",
        ):
            await ctx.respond(
                embed=EmbedBuilder(
                    title="Error",
                    description="This command can only be run in a help channel.",
                ).build(),
                ephemeral=True,
            )
            return

        if len(text) < 150:
            await ctx.respond(
                embed=EmbedBuilder(
                    title="Error",
                    description="You must enter at least 150 characters.",
                ).build(),
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)

        try:
            result = await asyncio.to_thread(detect_ai, text)
        except Exception as e:
            await ctx.respond(
                embed=EmbedBuilder(
                    title="Error",
                    description=f"An error occurred while running the command:\n\n{e}",
                ).build(),
                ephemeral=True,
            )
            raise

        embed_builder = EmbedBuilder(
            title="AI Detection Result",
            description=f"Overall result: {result}\n\nBelow is the word-by-word breakdown of the analysis.",
            footer="Retrieved from CopyLeaks by Homework Help",
            color=result.color_classification(),
        )

        embed_builder.fields = []
        for result_part in result.parts:
            embed_builder.fields.extend(
                [
                    ("Text", result_part.text_summary(), True),
                    ("Analysis", str(result_part), True),
                    ("\N{ZERO WIDTH SPACE}", "\N{ZERO WIDTH SPACE}", True),
                ],
            )

        await ctx.respond(embed=embed_builder.build(), ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(AI(bot))
