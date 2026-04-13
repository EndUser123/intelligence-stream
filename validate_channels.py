#!/usr/bin/env python3
"""Validate all tracked channels via yt-dlp (0 API quota)."""
import subprocess, concurrent.futures, sys

channels = [
    "https://www.youtube.com/@3blue1brown",
    "https://www.youtube.com/@AIJasonZ",
    "https://www.youtube.com/@AILABS-393",
    "https://www.youtube.com/@AILabs",
    "https://www.youtube.com/@AIexplained",
    "https://www.youtube.com/@AIforGood",
    "https://www.youtube.com/@AMD",
    "https://www.youtube.com/@AmazonScience",
    "https://www.youtube.com/@AndreMikalsen",
    "https://www.youtube.com/@AndrejKarpathy",
    "https://www.youtube.com/@Anthropic",
    "https://www.youtube.com/@ArgoAI",
    "https://www.youtube.com/@ArxivInsights",
    "https://www.youtube.com/@AssemblyAI",
    "https://www.youtube.com/@AureliusTjin",
    "https://www.youtube.com/@AuroraInnovation",
    "https://www.youtube.com/@BaiduApollo",
    "https://www.youtube.com/@BostonDynamics",
    "https://www.youtube.com/@CAISafety",
    "https://www.youtube.com/@Caltech",
    "https://www.youtube.com/@CambridgeUniversity",
    "https://www.youtube.com/@CarlVondrick",
    "https://www.youtube.com/@Chase-H-AI",
    "https://www.youtube.com/@CleverProgrammer",
    "https://www.youtube.com/@CloudWalk",
    "https://www.youtube.com/@CodeEmporium",
    "https://www.youtube.com/@ColeMedin",
    "https://www.youtube.com/@Computerphile",
    "https://www.youtube.com/@CoreyMSchafer",
    "https://www.youtube.com/@Coursera",
    "https://www.youtube.com/@CreatorMagicAI",
    "https://www.youtube.com/@DahuaAI",
    "https://www.youtube.com/@DataCamp",
    "https://www.youtube.com/@DataProfessor",
    "https://www.youtube.com/@DeepLearningAI",
    "https://www.youtube.com/@EPFL",
    "https://www.youtube.com/@ETHZurich",
    "https://www.youtube.com/@FAIR",
    "https://www.youtube.com/@Fireship",
    "https://www.youtube.com/@FutureOfLifeInstitute",
    "https://www.youtube.com/@GithubAwesome",
    "https://www.youtube.com/@GoogleDeepMind",
    "https://www.youtube.com/@GoogleResearch",
    "https://www.youtube.com/@GregIsenberg",
    "https://www.youtube.com/@Harvard",
    "https://www.youtube.com/@Huawei",
    "https://www.youtube.com/@IBMResearch",
    "https://www.youtube.com/@IKcodeIgorWnek",
    "https://www.youtube.com/@Indently",
    "https://www.youtube.com/@Intel",
    "https://www.youtube.com/@JCS",
    "https://www.youtube.com/@JeffHeaton",
    "https://www.youtube.com/@JordanHarrod",
    "https://www.youtube.com/@Kaggle",
    "https://www.youtube.com/@KennethLiao",
    "https://www.youtube.com/@Kurzgesagt",
    "https://www.youtube.com/@LangChain",
    "https://www.youtube.com/@LexClips",
    "https://www.youtube.com/@LiamOttley",
    "https://www.youtube.com/@LinusTechTips",
    "https://www.youtube.com/@LlamaIndex",
    "https://www.youtube.com/@LuukAlleman",
    "https://www.youtube.com/@MachineLearningMastery",
    "https://www.youtube.com/@MachineLearningTV",
    "https://www.youtube.com/@ManuAGI",
    "https://www.youtube.com/@MatthewBerman",
    "https://www.youtube.com/@MetaAI",
    "https://www.youtube.com/@MetalSole",
    "https://www.youtube.com/@MicrosoftDeveloper",
    "https://www.youtube.com/@MicrosoftResearch",
    "https://www.youtube.com/@Motional",
    "https://www.youtube.com/@NVIDIA",
    "https://www.youtube.com/@NVIDIAdeveloper",
    "https://www.youtube.com/@NateBJones",
    "https://www.youtube.com/@NickSaraev",
    "https://www.youtube.com/@Nuro",
    "https://www.youtube.com/@Octocode-ai",
    "https://www.youtube.com/@OpenAI",
    "https://www.youtube.com/@OpenRobotics",
    "https://www.youtube.com/@OxfordUniversity",
    "https://www.youtube.com/@PieterAbbeel",
    "https://www.youtube.com/@ProgrammingKnowledge",
    "https://www.youtube.com/@PromptEngineering",
    "https://www.youtube.com/@RAmjad",
    "https://www.youtube.com/@RayFernando1337",
    "https://www.youtube.com/@RickMulready",
    "https://www.youtube.com/@RiteshKV",
    "https://www.youtube.com/@RobertMilesAI",
    "https://www.youtube.com/@SamiSabirIdrissi",
    "https://www.youtube.com/@ShawhinTalebi",
    "https://www.youtube.com/@SirajRaval",
    "https://www.youtube.com/@SmarterEveryDay",
    "https://www.youtube.com/@SpeedyFoxAi",
    "https://www.youtube.com/@StanfordHAI",
    "https://www.youtube.com/@StanfordOnline",
    "https://www.youtube.com/@Supabase",
    "https://www.youtube.com/@TED",
    "https://www.youtube.com/@TechCrunch",
    "https://www.youtube.com/@TechTrainerTim",
    "https://www.youtube.com/@TechWithTim",
    "https://www.youtube.com/@TeslaAI",
    "https://www.youtube.com/@TimDillonShow",
    "https://www.youtube.com/@TomSc",
    "https://www.youtube.com/@TomScottGo",
    "https://www.youtube.com/@TwoBitDaVinci",
    "https://www.youtube.com/@TwoMinutePapers",
    "https://www.youtube.com/@UberAI",
    "https://www.youtube.com/@UniverseofAIz",
    "https://www.youtube.com/@Vsauce",
    "https://www.youtube.com/@Wanderloots",
    "https://www.youtube.com/@Waymo",
    "https://www.youtube.com/@YCombinator",
    "https://www.youtube.com/@YannicKilcher",
    "https://www.youtube.com/@YoshuaBengio",
    "https://www.youtube.com/@Zero2LaunchAI",
    "https://www.youtube.com/@Zoox",
    "https://www.youtube.com/@a16z",
    "https://www.youtube.com/@ai-foundations",
    "https://www.youtube.com/@ai-jason",
    "https://www.youtube.com/@aiexplained-official",
    "https://www.youtube.com/@aisamsonreal",
    "https://www.youtube.com/@aiwithbrandon",
    "https://www.youtube.com/@akshay_pachaar",
    "https://www.youtube.com/@alexk1919_ai",
    "https://www.youtube.com/@alexxmcfarland",
    "https://www.youtube.com/@anthropic-ai",
    "https://www.youtube.com/@apneareset",
    "https://www.youtube.com/@betterstack",
    "https://www.youtube.com/@briancasel",
    "https://www.youtube.com/@brianjenney",
    "https://www.youtube.com/@crashcourse",
    "https://www.youtube.com/@daveebbelaar",
    "https://www.youtube.com/@devforgehq",
    "https://www.youtube.com/@dylandavisAI",
    "https://www.youtube.com/@founders",
    "https://www.youtube.com/@freecodecamp",
    "https://www.youtube.com/@huggingface",
    "https://www.youtube.com/@iFlytek",
    "https://www.youtube.com/@iampauljames",
    "https://www.youtube.com/@iamseankochel",
    "https://www.youtube.com/@indydevdan",
    "https://www.youtube.com/@intheworldofai",
    "https://www.youtube.com/@janmarshalcoding",
    "https://www.youtube.com/@jonocatliff",
    "https://www.youtube.com/@khanacademy",
    "https://www.youtube.com/@leonvanzyl",
    "https://www.youtube.com/@lev-selector",
    "https://www.youtube.com/@lexfridman",
    "https://www.youtube.com/@markrober",
    "https://www.youtube.com/@matthew_berman",
    "https://www.youtube.com/@mattpocockuk",
    "https://www.youtube.com/@mrbeast",
    "https://www.youtube.com/@n8n-io",
    "https://www.youtube.com/@nateherk",
    "https://www.youtube.com/@priya-dwivedi",
    "https://www.youtube.com/@rajkkapadia",
    "https://www.youtube.com/@ratelimitedpod",
    "https://www.youtube.com/@rileybrownai",
    "https://www.youtube.com/@samwitteveen",
    "https://www.youtube.com/@sentdex",
    "https://www.youtube.com/@statquest",
    "https://www.youtube.com/@tachesteaches",
    "https://www.youtube.com/@unsupervised-learning",
    "https://www.youtube.com/@veritasium",
]

def check(url):
    r = subprocess.run(
        ["yt-dlp", "--js-runtimes", "node:C:/Program Files/nodejs/node",
         "--flat-playlist", "--print", "%(channel_id)s|%(playlist_count)s", url],
        capture_output=True, text=True, timeout=30
    )
    out = r.stdout.strip()
    err = r.stderr.strip()
    if r.returncode == 0 and out:
        parts = out.split("|")
        if len(parts) == 2:
            cid, pc = parts
            try:
                return (url, cid, int(pc), None)
            except ValueError:
                pass
        return (url, out, 0, None)
    hint = err.split("\n")[-1] if err else "unknown"
    return (url, None, 0, hint[:120])

good = bad = fail = 0
results = []
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(check, u): u for u in channels}
    for f in concurrent.futures.as_completed(futs):
        url, cid, count, err = f.result()
        results.append((url, cid, count, err))
        if cid and count > 1:
            good += 1
        elif cid and count <= 1:
            bad += 1
            print(f"BAD ({count} videos): {url}")
        else:
            fail += 1
            print(f"FAIL ({err[:80]}): {url}")

print(f"\n=== {good} good | {bad} bad (0-1 videos) | {fail} failed ===")
