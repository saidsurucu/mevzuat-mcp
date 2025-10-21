"""
Article-level keyword search for legislation.
Splits legislation into articles and searches for keywords within them.
"""
import re
from typing import List, Dict
from pydantic import BaseModel


class MaddeMatch(BaseModel):
    """A single article match result."""
    madde_no: str  # e.g., "1", "15", "142"
    madde_title: str  # e.g., "Amaç", "Tanımlar"
    madde_content: str  # Full article text
    match_count: int  # Number of keyword occurrences
    preview: str  # Short preview showing keyword in context


class ArticleSearchResult(BaseModel):
    """Search results within a legislation."""
    mevzuat_no: str
    mevzuat_tur: int
    keyword: str
    total_matches: int
    matching_articles: List[MaddeMatch]


def split_into_articles(markdown_content: str) -> List[Dict[str, str]]:
    """
    Split markdown content into individual articles.

    Returns list of dicts with keys: madde_no, madde_title, madde_content
    """
    articles = []

    # Split by article headers: **MADDE X –** or **MADDE X**- or **Madde X –**
    # Regex to match all formats (case-insensitive for MADDE/Madde):
    # - **MADDE 1 –** (dash inside **) - used in some laws
    # - **MADDE 1**- (dash outside **) - used in regulations
    # - **Madde 1 –** (title case) - used in some laws like CMK
    pattern = r'\*\*(?:MADDE|Madde)\s+(\d+)(?:\s*[–-])?\*\*\s*-?'

    # Find all article positions
    matches = list(re.finditer(pattern, markdown_content))

    if not matches:
        return []

    for i, match in enumerate(matches):
        madde_no = match.group(1)
        start_pos = match.start()

        # Find end position (start of next article or end of content)
        if i < len(matches) - 1:
            end_pos = matches[i + 1].start()
        else:
            end_pos = len(markdown_content)

        # Extract full article content
        article_text = markdown_content[start_pos:end_pos].strip()

        # Try to extract title (usually follows the article number)
        # Pattern: **MADDE X –** (1) or **Title** after article number
        title = ""
        lines = article_text.split('\n', 3)
        if len(lines) > 1:
            # Check if second line is a title (surrounded by **)
            second_line = lines[1].strip()
            if second_line.startswith('**') and second_line.endswith('**'):
                title = second_line.strip('*').strip()

        articles.append({
            'madde_no': madde_no,
            'madde_title': title,
            'madde_content': article_text
        })

    return articles


def _matches_query(content: str, query: str, case_sensitive: bool = False) -> tuple[bool, int]:
    """
    Check if content matches query with support for AND, OR, NOT, and exact match.

    Query syntax:
    - "exact phrase" - Exact match with quotes
    - word1 AND word2 - Both words must be present (AND must be uppercase)
    - word1 OR word2 - At least one word must be present (OR must be uppercase)
    - word1 NOT word2 - word1 present but word2 must not be (NOT must be uppercase)
    - Combinations: "exact phrase" AND word1 OR word2 NOT word3

    Returns:
        (matches: bool, score: int) - Whether content matches and relevance score
    """
    # Parse exact phrases (quoted) - before any case conversion
    exact_phrases = re.findall(r'"([^"]*)"', query)

    # Remove exact phrases from query for further parsing
    temp_query = query
    for phrase in exact_phrases:
        temp_query = temp_query.replace(f'"{phrase}"', '')

    # Apply case sensitivity
    search_content = content if case_sensitive else content.lower()

    # Split by logical operators while preserving them (operators are case sensitive - must be uppercase)
    tokens = re.split(r'\s+(AND|OR|NOT)\s+', temp_query)
    tokens = [t.strip() for t in tokens if t.strip()]

    # Build evaluation stack
    # Start with True (neutral for AND chains)
    result = None
    current_op = 'AND'  # Default operator
    score = 0

    # Check exact phrases first
    for phrase in exact_phrases:
        phrase_search = phrase if case_sensitive else phrase.lower()
        if phrase_search in search_content:
            score += search_content.count(phrase_search) * 2  # Exact matches worth more
            if result is None:
                result = True
        else:
            if current_op == 'AND' or result is None:
                return False, 0  # Required phrase not found

    # Process remaining tokens
    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in ('AND', 'OR', 'NOT'):
            current_op = token
            i += 1
            continue

        # Token is a search term - apply case sensitivity
        search_token = token if case_sensitive else token.lower()
        term_found = search_token in search_content
        term_count = search_content.count(search_token) if term_found else 0

        if current_op == 'AND':
            if result is None:
                result = term_found
                if term_found:
                    score += term_count
            else:
                result = result and term_found
                if term_found:
                    score += term_count
                else:
                    return False, 0  # AND condition failed

        elif current_op == 'OR':
            if result is None:
                result = term_found
            else:
                result = result or term_found
            if term_found:
                score += term_count

        elif current_op == 'NOT':
            if term_found:
                return False, 0  # NOT condition failed
            # NOT doesn't affect score

        i += 1

    # If no terms were processed, default to False
    if result is None:
        result = False

    return result, score


def search_articles_by_keyword(
    markdown_content: str,
    keyword: str,
    case_sensitive: bool = False,
    max_results: int = 50
) -> List[MaddeMatch]:
    """
    Search for keyword within articles with support for advanced operators.

    Query syntax:
    - Simple keyword: "yatırımcı"
    - Exact phrase: "mali sıkıntı"
    - AND operator: yatırımcı AND tazmin
    - OR operator: yatırımcı OR müşteri
    - NOT operator: yatırımcı NOT kurum
    - Combinations: "mali sıkıntı" AND yatırımcı NOT kurum

    Args:
        markdown_content: Full legislation content in markdown
        keyword: Search query with optional operators (AND, OR, NOT, "exact phrase")
        case_sensitive: Whether to match case
        max_results: Maximum number of matching articles to return

    Returns:
        List of matching articles sorted by relevance (score based on match count)
    """
    articles = split_into_articles(markdown_content)
    matches = []

    for article in articles:
        content = article['madde_content']

        # Check if article matches query
        matches_query, score = _matches_query(content, keyword, case_sensitive)

        if matches_query and score > 0:
            # Generate preview (first occurrence of a search term)
            search_content = content if case_sensitive else content.lower()
            search_keyword = keyword if case_sensitive else keyword.lower()

            # Try to find first quoted phrase or first word
            preview_terms = re.findall(r'"([^"]*)"', search_keyword)
            if not preview_terms:
                # Use first word (excluding operators)
                words = re.split(r'\s+(?:AND|OR|NOT)\s+', search_keyword)
                preview_terms = [w.strip() for w in words if w.strip() and w.strip() not in ('AND', 'OR', 'NOT')]

            preview = ""
            if preview_terms:
                first_term = preview_terms[0] if case_sensitive else preview_terms[0].lower()
                if first_term in search_content:
                    keyword_pos = search_content.find(first_term)
                    start = max(0, keyword_pos - 100)
                    end = min(len(content), keyword_pos + len(first_term) + 100)
                    preview = content[start:end]

                    if start > 0:
                        preview = "..." + preview
                    if end < len(content):
                        preview = preview + "..."

            if not preview:
                preview = content[:200] + "..."

            matches.append(MaddeMatch(
                madde_no=article['madde_no'],
                madde_title=article['madde_title'],
                madde_content=content,
                match_count=score,
                preview=preview
            ))

    # Sort by score (most relevant first)
    matches.sort(key=lambda x: x.match_count, reverse=True)

    return matches[:max_results]


def format_search_results(result: ArticleSearchResult) -> str:
    """Format search results as readable text."""
    output = []
    output.append(f"Keyword: '{result.keyword}'")
    output.append(f"Total matching articles: {result.total_matches}")
    output.append("")

    for i, match in enumerate(result.matching_articles, 1):
        output.append(f"=== MADDE {match.madde_no} ===")
        if match.madde_title:
            output.append(f"Title: {match.madde_title}")
        output.append(f"Matches: {match.match_count}")
        output.append("")
        output.append("Full content:")
        output.append(match.madde_content)
        output.append("")

    return "\n".join(output)
