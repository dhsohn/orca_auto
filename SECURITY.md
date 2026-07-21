# Security policy

orca_auto runs local processes, reads configuration files, writes calculation
artifacts, and can optionally send Discord notifications. Treat credentials,
private structures, and raw calculation outputs as sensitive unless they are
explicitly public.

## Supported versions

Security fixes are handled on the current `main` branch first. If a tagged
release is affected and a backport is practical, the release notes will say so.

## Reporting a vulnerability

Do not post secrets, private structures, full private logs, or working exploit
details in a public issue.

Preferred reporting paths:

1. Use GitHub private vulnerability reporting or a private security advisory if
   it is available for the repository.
2. If private reporting is not available, contact the maintainer through GitHub
   with a minimal request for a private channel. Do not include sensitive details
   in the public request.

Please include:

- affected commit or release;
- operating system/runtime context;
- the vulnerable command, configuration key, or workflow surface;
- minimal reproduction steps using sanitized paths and dummy credentials;
- impact assessment, such as credential exposure, path traversal, unsafe process
  execution, or unsafe publication of private artifacts.

## Sensitive data guidelines

Before posting issues, PRs, logs, fixtures, or examples, remove:

- Discord bot tokens and channel IDs;
- shell environment variables containing credentials;
- private workstation or cluster usernames when not needed;
- proprietary ORCA output or unpublished structures;
- absolute private data paths;
- scheduler logs that reveal account, allocation, or cluster policy details.

The repository CI runs secret scanning, but scanning is a last line of defense.
Contributors remain responsible for reviewing artifacts before they are committed
or posted publicly.

## Security-relevant areas

The following classes of issues are security-relevant for orca_auto:

- path traversal or writing outside configured runtime roots;
- unsafe acceptance of Windows, `/mnt/<drive>`, relative, or `.exe` executable
  paths where Linux-only executable policy is expected;
- shell injection or unsafe process invocation;
- messenger bot token leakage or accidental notification to the wrong destination;
- logs, reports, examples, or fixtures that expose private structures or secrets;
- GitHub Actions or release-process changes that weaken secret handling.

## Non-security issues

The following are usually regular support or bug-report issues rather than
security vulnerabilities:

- ORCA convergence failure;
- chemically invalid inputs;
- unsupported local scheduler configuration;
- site-specific MPI/module problems;
- expected rejection of Windows-style executable paths.
