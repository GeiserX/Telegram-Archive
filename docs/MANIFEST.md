# Documentation Manifest

**Version:** 7.0 | **Generated:** 2026-02-17

## Overview

This manifest lists all documentation files, their purposes, intended audiences, and maintenance notes.

## Documentation Files

### Navigation & Index

| File | Purpose | Lines | Status |
|------|---------|-------|--------|
| **README.md** | Navigation hub for all documentation | ~250 | ✓ New |
| **MANIFEST.md** | This file - file inventory and maintenance guide | ~150 | ✓ New |

### Core Documentation

| File | Purpose | Lines | Audience | Status |
|------|---------|-------|----------|--------|
| **v70-quick-reference.md** | Quick start, API reference, troubleshooting, FAQ | ~300 | Everyone | ✓ New |
| **codebase-summary.md** | Technical overview of modules and components | 191 | Developers, PM | ✓ New |
| **system-architecture.md** | Design patterns, flows, deployment architecture | 389 | Developers, DevOps | ✓ New |
| **code-standards.md** | Development guidelines, patterns, testing | 600 | Developers | ✓ New |
| **project-overview-pdr.md** | Product requirements, features, acceptance criteria | 403 | PM, Developers | ✓ New |

### Reference Documentation

| File | Purpose | Lines | Status |
|------|---------|-------|--------|
| **CHANGELOG.md** | Complete version history with migration notes | 1,055 | ✓ Updated (v7.0 entry) |
| **ROADMAP.md** | Future features and planned enhancements | 206 | ✓ Existing |

## File Statistics

```
Total Files: 8
Total Lines: 3,100+
Total Size: 120K

Compliance: All files under 800 LOC limit (except reference docs)
- codebase-summary.md:      191 lines (77% under limit)
- system-architecture.md:   389 lines (51% under limit)
- code-standards.md:        600 lines (25% under limit)
- project-overview-pdr.md:  403 lines (50% under limit)
- v70-quick-reference.md:  ~300 lines (63% under limit)
- README.md:               ~250 lines (69% under limit)
- CHANGELOG.md:           1,055 lines (reference doc)
- ROADMAP.md:              206 lines (existing doc)
```

## Content Organization

### By Feature (v7.0)

**Search Enhancement**
- codebase-summary.md (API overview section)
- system-architecture.md (Search flow section)
- code-standards.md (Frontend patterns section)
- v70-quick-reference.md (What's new section)

**Media Gallery**
- codebase-summary.md (Frontend features section)
- system-architecture.md (Media gallery flow section)
- code-standards.md (Vue patterns section)
- v70-quick-reference.md (Quick reference section)

**Transaction Accounting**
- codebase-summary.md (Database models, API overview)
- system-architecture.md (Transaction detection flow)
- code-standards.md (Database patterns, testing)
- project-overview-pdr.md (Functional requirements)
- v70-quick-reference.md (How it works section)

**UX Improvements**
- code-standards.md (Frontend patterns)
- v70-quick-reference.md (Keyboard shortcuts)
- system-architecture.md (Performance targets)

### By Audience

**For Users**
1. v70-quick-reference.md (overview, usage, FAQ)
2. README.md (navigation to specific docs)
3. CHANGELOG.md (what's new in releases)

**For Developers**
1. v70-quick-reference.md (quick API ref)
2. code-standards.md (development patterns)
3. system-architecture.md (design details)
4. codebase-summary.md (module overview)

**For DevOps/Operators**
1. v70-quick-reference.md (migration checklist)
2. CHANGELOG.md (migration notes)
3. system-architecture.md (deployment)

**For Project Managers**
1. project-overview-pdr.md (requirements, features)
2. CHANGELOG.md (release notes)
3. codebase-summary.md (technical overview)

**For API Consumers**
1. v70-quick-reference.md (endpoint quick ref)
2. system-architecture.md (API patterns & examples)
3. code-standards.md (error handling, validation)

## Maintenance Guidelines

### Update Schedule

- **Security issues**: Immediate
- **Feature additions**: With each release
- **API changes**: Immediately after code change
- **Bug fixes**: Within one release cycle
- **Examples**: Quarterly review for accuracy
- **Links**: Verify quarterly

### Who Updates What

| File | Owner | Reviewer | Trigger |
|------|-------|----------|---------|
| CHANGELOG.md | Feature author | Maintainer | Each commit |
| v70-quick-reference.md | Feature author | Documentation team | Feature release |
| code-standards.md | Code reviewer | Maintainer | Code pattern changes |
| system-architecture.md | Architect | Maintainer | Design changes |
| project-overview-pdr.md | Product owner | PM | Requirement changes |
| codebase-summary.md | Documentation | Maintainer | Major refactoring |

### Review Checklist

Before updating any documentation:
- [ ] Read existing content to avoid duplication
- [ ] Check for consistency with related docs
- [ ] Verify all code examples still work
- [ ] Update cross-references if needed
- [ ] Check line count compliance (800 LOC max)
- [ ] Verify all links work
- [ ] Run spell check
- [ ] Get review before merging

### Adding New Sections

When adding new documentation:
1. Start with README.md (update navigation)
2. Create new doc or update existing
3. Keep under 800 lines (split if needed)
4. Add cross-references
5. Include in CHANGELOG
6. Update MANIFEST.md

### Splitting Large Files

When a doc approaches 800 lines:
1. Identify semantic boundaries
2. Create subdirectory: `docs/{topic}/`
3. Create `index.md` with overview
4. Split into `part-1.md`, `part-2.md`, etc.
5. Link from original location
6. Update navigation

## Quality Standards

### Code Examples
- Must be runnable or representative
- Include imports and necessary context
- Type hints required
- Error handling shown
- Tested against actual code

### API Documentation
- Method signature shown
- Parameters documented with types
- Return value documented
- Example request and response
- Error codes listed
- Rate limits noted

### Architecture Diagrams
- ASCII format for text
- Clear labels
- Legend if needed
- Accurate to current code

### Explanations
- Active voice preferred
- No jargon without definition
- Linked to related docs
- Examples provided
- Progressive detail levels

## Cross-Reference Map

**codebase-summary.md references:**
- system-architecture.md (for design details)
- code-standards.md (for patterns)
- project-overview-pdr.md (for features)

**system-architecture.md references:**
- code-standards.md (for implementation)
- codebase-summary.md (for modules)
- v70-quick-reference.md (for quick ref)

**code-standards.md references:**
- system-architecture.md (for design context)
- project-overview-pdr.md (for requirements)

**project-overview-pdr.md references:**
- code-standards.md (for implementation)
- system-architecture.md (for technical design)
- CHANGELOG.md (for version history)

**v70-quick-reference.md references:**
- README.md (for full docs index)
- system-architecture.md (for details)
- CHANGELOG.md (for migration)

## Version Control

### Commit Guidelines

When updating docs:
```
docs: update {filename} - {description}

- Specific change 1
- Specific change 2
- Specific change 3

Fixes #123
Relates to docs/v70-quick-reference.md
```

### PR Template

Include in documentation PRs:
- [ ] Updated relevant doc files
- [ ] Verified all code examples
- [ ] Updated cross-references
- [ ] Checked line count compliance
- [ ] Verified links work
- [ ] Added CHANGELOG entry

## Documentation Debt

Current documentation status: **COMPLETE for v7.0**

No known gaps or outdated sections.

Planned improvements:
- Add interactive examples (v7.1+)
- Create video tutorials (v7.1+)
- Add more deployment examples (v7.1+)
- Create admin guide (v7.1+)

## Feedback & Issues

Report documentation issues:
1. Title format: `docs: {filename} - {issue}`
2. Include: What's wrong, where to find it, suggested fix
3. Reference related code/files
4. Examples of correct behavior

Example:
```
docs: v70-quick-reference.md - API endpoint returns wrong format

The transaction export endpoint returns JSON but docs say CSV.
Check /api/chats/{id}/transactions/export actual response.
```

## Quick Links

- **Documentation Index**: [README.md](./README.md)
- **Quick Start**: [v70-quick-reference.md](./v70-quick-reference.md)
- **Architecture**: [system-architecture.md](./system-architecture.md)
- **Development**: [code-standards.md](./code-standards.md)
- **Specifications**: [project-overview-pdr.md](./project-overview-pdr.md)
- **Version History**: [CHANGELOG.md](./CHANGELOG.md)
- **Future Plans**: [ROADMAP.md](./ROADMAP.md)

## Document History

| Date | Version | Changes |
|------|---------|---------|
| 2026-02-17 | 7.0 | Initial manifest for v7.0 docs |

---

**Maintained by:** Telegram Archive Documentation Team
**Last Updated:** 2026-02-17
**Next Review:** 2026-05-17
