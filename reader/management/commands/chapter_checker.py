from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from reader.models import Group, Series, Volume, Chapter, ChapterIndex
from api.api import random_chars, clear_pages_cache, chapter_post_process

from datetime import datetime, timezone
from bs4 import BeautifulSoup
import asyncio
import aiohttp
from datetime import datetime
import pytz
import re
import os
import io
import json
import shutil
import zipfile
import traceback


class Command(BaseCommand):
    help = "Import new chapters from JaiminisBox and Mangadex"

    def add_arguments(self, parser):
        # # Positional arguments
        parser.add_argument("--lookup", nargs="?", default="all")
        parser.add_argument("--dl", nargs="?")
        parser.add_argument("--series", nargs="?")
        parser.add_argument("--latest_chap", nargs="?")
        parser.add_argument("--update", nargs="?")
        parser.add_argument("--folder", nargs="?")
        parser.add_argument("--group", nargs="?")

        # # Named (optional) arguments
        # parser.add_argument(
        #     '--jb',
        #     action='store_true',
        #     help='lookup specific site. options: jb or md',
        # )
        # parser.add_argument(
        #     '--jb',
        #     action='store_true',
        #     help='lookup specific site. options: jb or md',
        # )

    def __init__(self):
        self.browser = None
        self.page = None
        if hasattr(settings, "SCRAPER_BLACKLIST_FILE"):
            if not os.path.exists(settings.SCRAPER_BLACKLIST_FILE):
                with open(settings.SCRAPER_BLACKLIST_FILE, "w") as f:
                    json.dump([], f)
                    blacklist = []
            else:
                with open(settings.SCRAPER_BLACKLIST_FILE) as f:
                    blacklist = json.load(f)
            self.blacklist_jb = {
                "Kaguya-Wants-To-Be-Confessed-To": blacklist,
                "We-Want-To-Talk-About-Kaguya": [],
                "Kaguya-Wants-To-Be-Confessed-To-Official-Doujin": [],
            }
        self.jaiminisbox_manga = {
            "Kaguya-Wants-To-Be-Confessed-To": "https://jaiminisbox.com/reader/series/kaguya-wants-to-be-confessed-to/",
            "We-Want-To-Talk-About-Kaguya": "https://jaiminisbox.com/reader/series/we-want-to-talk-about-kaguya/",
            "Kaguya-Wants-To-Be-Confessed-To-Official-Doujin": "https://jaiminisbox.com/reader/series/kaguya-wants-to-be-confessed-to-official-doujin/",
        }
        self.mangadex_manga_id = {
            "Kaguya-Wants-To-Be-Confessed-To": 17274,
            "We-Want-To-Talk-About-Kaguya": 29338,
            "Kaguya-Wants-To-Be-Confessed-To-Official-Doujin": 28363,
        }
        self.jb_group = 3
        self.md_group = 2

    def create_chapter_obj(self, chapter, group, series, latest_volume, title):
        chapter_number = float(chapter)
        existing_chapter = Chapter.objects.filter(
            chapter_number=chapter_number, series=series
        ).first()
        chapter_folder_numb = f"{int(chapter_number):04}"
        chapter_folder_numb += (
            f"-{str(chapter_number).rsplit('.')[1]}_"
            if not str(chapter_number).endswith("0")
            else "_"
        )
        if not existing_chapter:
            uid = chapter_folder_numb + random_chars()
        else:
            uid = existing_chapter.folder
        Chapter.objects.create(
            chapter_number=chapter_number,
            group=group,
            series=series,
            folder=uid,
            title=title,
            volume=latest_volume,
            uploaded_on=datetime.utcnow().replace(tzinfo=timezone.utc),
        )
        chapter_folder = os.path.join(
            settings.MEDIA_ROOT, "manga", series.slug, "chapters", uid
        )
        os.makedirs(os.path.join(chapter_folder, str(group.id)))
        os.makedirs(os.path.join(chapter_folder, f"{str(group.id)}_shrunk"))
        os.makedirs(os.path.join(chapter_folder, f"{str(group.id)}_shrunk_blur"))
        clear_pages_cache()
        return chapter_folder, str(group.id)

    async def mangadex_download(self, chapters, series, group, latest_volume, url=""):
        for chapter in chapters:
            if not chapters[chapter]:
                print(f"Could not download chapter {chapter}.")
                continue
            chapter_pages = chapters[chapter][1]
            chapter_folder, group_folder = self.create_chapter_obj(
                chapter, group, series, latest_volume, chapters[chapter][0]
            )
            ch = Chapter.objects.get(
                series=series, chapter_number=float(chapter), group=group
            )
            padding = len(str(len(chapter_pages)))
            print(f"Downloading chapter {chapter}...")
            print(f"Found {len(chapter_pages)} pages...")
            async with aiohttp.ClientSession() as session:
                for idx, page in enumerate(chapter_pages):
                    extension = page.rsplit(".", 1)[1]
                    page_file = f"{str(idx+1).zfill(padding)}.{extension}"
                    async with session.get(page) as resp:
                        if resp.status == 200:
                            page_content = await resp.read()
                            with open(
                                os.path.join(chapter_folder, group_folder, page_file),
                                "wb",
                            ) as f:
                                f.write(page_content)
                        else:
                            print("failed at mangadex_download", idx, page)
            chapter_post_process(ch, update_version=False)

    async def get_chapter_list(self, series_id):
        md_series_api = f"https://mangadex.org/api/?id={series_id}&type=manga"
        chapter_dict = {}
        async with aiohttp.ClientSession() as session:
            async with session.get(md_series_api) as resp:
                if resp.status == 200:
                    data = await resp.text()
                    api_data = json.loads(data)
                    for ch in api_data["chapter"]:
                        if api_data["chapter"][ch]["lang_code"] == "gb":
                            if api_data["chapter"][ch]["chapter"] not in chapter_dict:
                                chapter_dict[api_data["chapter"][ch]["chapter"]] = ch
        return chapter_dict

    async def get_chapter_pages(self, series_id, chapter_number):
        chapter_id = None
        async with aiohttp.ClientSession() as session:
            series_chapters = await self.get_chapter_list(series_id)
            if chapter_number in series_chapters:
                chapter_id = series_chapters[chapter_number]
            else:
                return None
            async with session.get(
                f"https://mangadex.org/api/?id={chapter_id}&server=null&type=chapter"
            ) as resp:
                if resp.status == 200:
                    data = await resp.text()
                    api_data = json.loads(data)
                    domain = (
                        api_data["server"]
                        if not api_data["server"].startswith("/")
                        else f"https://mangadex.org" + api_data["server"]
                    )
                    chapter_data = (
                        api_data["title"],
                        [
                            f"{domain}{api_data['hash']}/{page}"
                            for page in api_data["page_array"]
                        ],
                        chapter_id,
                    )
                    return chapter_data
                else:
                    print("failed at get_chapter_pages")
        return None

    async def mangadex_checker(
        self, downloaded_chapters, series_slug, latest_volume, latest_only=False
    ):
        if series_slug == "Kaguya-Wants-To-Be-Confessed-To":
            tz = pytz.timezone("Japan")
            if datetime.now(tz).weekday() < 3:
                return
        chapters = {}
        chapter_list = await self.get_chapter_list(self.mangadex_manga_id[series_slug])
        group = Group.objects.get(pk=self.md_group)
        series = Series.objects.get(slug=series_slug)
        for chapter in chapter_list:
            if str(float(chapter)) in self.blacklist_jb[series.slug]:
                continue
            if str(float(chapter)) not in downloaded_chapters:
                print(f"Found new chapter ({chapter}) on MangaDex for {series_slug}.")
                chapters[chapter] = await self.get_chapter_pages(
                    self.mangadex_manga_id[series_slug], chapter
                )
        await self.mangadex_download(chapters, series, group, latest_volume)

    async def jaiminis_box_checker(
        self, downloaded_chapters, series, latest_volume, url, latest_chap=None
    ):
        chapters = {}
        group = Group.objects.get(pk=self.jb_group)
        series = Series.objects.get(slug=series)
        if not latest_chap:
            async with aiohttp.ClientSession() as session:
                print(url)
                async with session.get(url) as resp:
                    if resp.status == 200:
                        webpage = await resp.text()
                        soup = BeautifulSoup(webpage, "html.parser")
                        for chapter in soup.select(".list .group .element"):
                            chapter_regex = re.search(
                                r"^Chapter (\d*\.?\d*): (.*)$",
                                chapter.select(".title")[0].text,
                            )
                            chap_numb = chapter_regex.group(1)
                            if (
                                str(float(chap_numb)) in downloaded_chapters
                                or str(float(chap_numb))
                                in self.blacklist_jb[series.slug]
                            ):
                                continue
                            else:
                                print(
                                    f"Found new chapter ({chap_numb}) on Jaiminisbox for {series}."
                                )
                                chapter_dl_url = chapter.select(".icon_wrapper a")[0][
                                    "href"
                                ]
                                chapters[chap_numb] = {
                                    "title": chapter_regex.group(2),
                                    "url": chapter_dl_url,
                                }
                    else:
                        print(
                            f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Failed to reach JB page for {series}. Response status: {resp.status}"
                        )
        else:
            latest_chap_slug = latest_chap.replace(".", "/")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://jaiminisbox.com/reader/read/{series.slug.lower()}/en/0/{latest_chap_slug}/page/1"
                ) as resp:
                    if resp.status == 200:
                        webpage = await resp.text()
                        soup = BeautifulSoup(webpage, "html.parser")
                        title = (
                            soup.select(".tbtitle .text a")[1]
                            .text.split(":", 1)[1]
                            .strip()
                        )
                        chapters[str(latest_chap)] = {
                            "title": title,
                            "url": f"https://jaiminisbox.com/reader/download/{series.slug.lower()}/en/0/{latest_chap_slug}/",
                        }
                    else:
                        print(resp.status)
        for chapter in chapters:
            chapter_url = chapters[chapter]["url"]
            if (
                "(Digital)" in chapters[chapter]["title"]
                and str(float(int(float(chapter)))) in downloaded_chapters
            ):
                if hasattr(settings, SCRAPER_BLACKLIST_FILE) and os.path.exists(
                    settings.SCRAPER_BLACKLIST_FILE
                ):
                    with open(settings.SCRAPER_BLACKLIST_FILE, "r+") as f:
                        blacklist = json.load(f)
                        f.seek(0)
                        f.truncate()
                        blacklist.append(str(chapter))
                        json.dump(blacklist, f)
                    chapter = str(float(int(float(chapter))))
            if str(float(chapter)) not in downloaded_chapters:
                reupdating = False
                chapter_folder, group_folder = self.create_chapter_obj(
                    chapter, group, series, latest_volume, chapters[chapter]["title"]
                )
                ch = Chapter.objects.get(
                    series=series, group=self.jb_group, chapter_number=float(chapter)
                )
                print(f"Downloading chapter {chapter}...")
            else:
                reupdating = True
                ch = Chapter.objects.get(
                    series=series, group=self.jb_group, chapter_number=float(chapter)
                )
                chapter_folder = os.path.join(
                    settings.MEDIA_ROOT, "manga", series.slug, "chapters", ch.folder
                )
                group_folder = str(self.jb_group)
                print(f"Reupdating chapter pages for {chapter}...")
                for f in os.listdir(os.path.join(chapter_folder, group_folder)):
                    os.remove(os.path.join(chapter_folder, group_folder, f))
            async with aiohttp.ClientSession() as session:
                async with session.get(chapter_url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
                            all_pages = sorted(zip_file.namelist())
                            padding = len(str(len(all_pages)))
                            for idx, page in enumerate(all_pages):
                                extension = page.rsplit(".", 1)[1]
                                page_file = f"{str(idx+1).zfill(padding)}.{extension}"
                                with open(
                                    os.path.join(
                                        chapter_folder, group_folder, page_file
                                    ),
                                    "wb",
                                ) as f:
                                    f.write(zip_file.read(page))
                        chapter_post_process(ch, update_version=reupdating)

    def handle(self, *args, **options):
        loop = asyncio.get_event_loop()
        if options["update"] and options["group"]:
            chapter = Chapter.objects.get(
                series__slug=options["series"],
                chapter_number=float(options["update"]),
                group=options["group"],
            )
            chapter_post_process(chapter)
        elif options["dl"] == "jb" and options["series"] and options["latest_chap"]:
            latest_volume = (
                Volume.objects.filter(series__slug=options["series"])
                .order_by("-volume_number")[0]
                .volume_number
            )
            chapters = set(
                [
                    str(chapter.chapter_number)
                    for chapter in Chapter.objects.filter(
                        series__slug=options["series"], group=self.jb_group
                    )
                ]
            )
            loop.run_until_complete(
                self.jaiminis_box_checker(
                    chapters,
                    options["series"],
                    latest_volume,
                    self.jaiminisbox_manga[options["series"]],
                    latest_chap=options["latest_chap"],
                )
            )
        else:
            if options["lookup"] == "all" or options["lookup"] == "jb":
                for manga in self.jaiminisbox_manga:
                    latest_volume = (
                        Volume.objects.filter(series__slug=manga)
                        .order_by("-volume_number")[0]
                        .volume_number
                    )
                    chapters = set(
                        [
                            str(chapter.chapter_number)
                            for chapter in Chapter.objects.filter(
                                series__slug=manga, group=self.jb_group
                            )
                        ]
                    )
                    loop.run_until_complete(
                        self.jaiminis_box_checker(
                            chapters,
                            manga,
                            latest_volume,
                            self.jaiminisbox_manga[manga],
                        )
                    )
            if options["lookup"] == "all" or options["lookup"] == "md":
                for manga in self.mangadex_manga_id:
                    latest_volume = (
                        Volume.objects.filter(series__slug=manga)
                        .order_by("-volume_number")[0]
                        .volume_number
                    )
                    if manga == "Kaguya-Wants-To-Be-Confessed-To":
                        chapters = [
                            str(chapter.chapter_number)
                            for chapter in Chapter.objects.filter(series__slug=manga)
                        ]
                    else:
                        chapters = [
                            str(chapter.chapter_number)
                            for chapter in Chapter.objects.filter(
                                series__slug=manga, group=self.md_group
                            )
                        ]
                    loop.run_until_complete(
                        self.mangadex_checker(chapters, manga, latest_volume)
                    )
