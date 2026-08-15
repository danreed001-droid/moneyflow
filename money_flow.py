def main():
    report = build_report()
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## Money Flow Snapshot\n\n```\n" + report + "\n```\n")

    # Write the latest report to a file in the repo so it can be read back
    # (e.g. by a Claude scheduled task fetching the public raw URL) without
    # needing any GitHub API/auth access.
    with open("latest.txt", "w") as f:
        f.write(report + "\n")

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        send_ntfy(report, topic)
    else:
        print("[info] NTFY_TOPIC not set -- skipping push notification.")


if __name__ == "__main__":
    main()
