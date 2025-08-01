"""
Forum Wisdom Miner - Streamlined Application

Efficient forum thread analysis application with focused architecture
for accurate analytical data extraction and conversational querying.
Optimized for consumer hardware with 8GB RAM.
"""

import logging
import os
import threading
import time
import weakref
from typing import Dict, List, Optional

from flask import Flask, Response, jsonify, render_template, request

# Import our modular components
from analytics.query_analytics import ConversationalQueryProcessor
from analytics.thread_analyzer import ThreadAnalyzer
from config.settings import (
    BASE_TMP_DIR,
    EMBEDDING_BATCH_SIZE,
    FEATURES,
    MAX_LOGGED_PROMPT_LENGTH,
    MAX_WORKERS,
    OLLAMA_BASE_URL,
    OLLAMA_CHAT_MODEL,
    OLLAMA_EMBED_MODEL,
    QUERY_PROCESSOR_CACHE_SIZE,
    THREADS_DIR,
)
from processing.thread_processor import ThreadProcessor
from search.query_processor import QueryProcessor
from utils.file_utils import get_thread_dir, safe_read_json
from utils.helpers import normalize_url
from utils.memory_optimizer import MemoryMonitor, get_memory_status
from utils.question_history import get_question_history
from utils.shared_data_manager import get_data_manager

# -------------------- Logging Configuration --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_TMP_DIR, 'app.log'), mode='a'),
    ],
)
logger = logging.getLogger(__name__)

# -------------------- Flask Application --------------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'forum-wisdom-miner-secret-key')

# -------------------- Global Components --------------------
# Initialize main processor and memory monitoring
thread_processor = ThreadProcessor()
memory_monitor = MemoryMonitor()


# Thread-safe LRU cache for query processors
class QueryProcessorCache:
    """Thread-safe LRU cache for QueryProcessor instances with automatic cleanup."""

    def __init__(self, max_size: int = QUERY_PROCESSOR_CACHE_SIZE):
        self._cache = {}
        self._access_order = []
        self._max_size = max_size
        self._lock = threading.RLock()

    def get(self, thread_key: str) -> QueryProcessor:
        """Get or create a query processor for a thread."""
        with self._lock:
            if thread_key in self._cache:
                # Move to end (most recently used)
                self._access_order.remove(thread_key)
                self._access_order.append(thread_key)
                return self._cache[thread_key]

            # Create new processor
            thread_dir = get_thread_dir(thread_key)
            processor = QueryProcessor(thread_dir)

            # Add to cache
            self._cache[thread_key] = processor
            self._access_order.append(thread_key)

            # Cleanup if needed
            self._cleanup_if_needed()

            return processor

    def _cleanup_if_needed(self):
        """Remove least recently used items if cache is full."""
        while len(self._cache) > self._max_size:
            lru_key = self._access_order.pop(0)
            if lru_key in self._cache:
                del self._cache[lru_key]
                logger.debug(f'Removed query processor for thread {lru_key} from cache')

    def clear(self):
        """Clear all cached processors."""
        with self._lock:
            self._cache.clear()
            self._access_order.clear()

    def size(self) -> int:
        """Get current cache size."""
        return len(self._cache)


query_processor_cache = QueryProcessorCache()


# -------------------- Utility Functions --------------------
def get_query_processor(thread_key: str) -> QueryProcessor:
    """Get or create a query processor for a specific thread.

    Args:
        thread_key: Thread identifier

    Returns:
        QueryProcessor instance for the thread
    """
    return query_processor_cache.get(thread_key)


def list_available_threads() -> List[str]:
    """List all available processed threads.

    Returns:
        List of thread keys
    """
    try:
        summaries = thread_processor.list_processed_threads()
        return [summary['thread_key'] for summary in summaries]
    except Exception as e:
        logger.error(f'Error listing threads: {e}')
        return []


def validate_request_parameters(
    prompt: str, url: str, existing_thread: str
) -> Optional[str]:
    """Validate query request parameters.

    Args:
        prompt: User query
        url: Thread URL
        existing_thread: Existing thread key

    Returns:
        Error message if validation fails, None if valid
    """
    if not prompt or not prompt.strip():
        return 'Prompt is required'

    if not url and not existing_thread:
        return 'Either URL or existing thread must be provided'

    if url and existing_thread:
        return 'Cannot specify both URL and existing thread'

    return None


def process_existing_thread(
    thread_key: str, reprocess: bool, message_queue, progress_update
) -> tuple:
    """Process an existing thread with optional reprocessing.

    Args:
        thread_key: Thread identifier
        reprocess: Whether to reprocess the thread
        message_queue: Queue for progress messages
        progress_update: Progress callback function

    Returns:
        Tuple of (thread_key, processing_results)
    """
    thread_dir = get_thread_dir(thread_key)
    if not os.path.exists(thread_dir):
        message_queue.put(
            f"Error: Thread '{thread_key}' not found. Please delete and recreate with URL.\n"
        )
        return thread_key, None

    if reprocess:
        message_queue.put(f'Reprocessing existing thread: {thread_key}\n')
        message_queue.put('Re-parsing HTML files and rebuilding indexes...\n\n')

        # Reprocess existing thread (no re-download)
        thread_key, processing_results = thread_processor.reprocess_existing_thread(
            thread_key, progress_update
        )

        message_queue.put('Thread reprocessed successfully!\n')
        message_queue.put(
            f'Posts processed: {processing_results.get("posts_count", 0)}\n'
        )
    else:
        # Use existing thread as-is
        message_queue.put(f'Using existing thread: {thread_key}\n')
        processing_results = {'thread_key': thread_key, 'from_cache': True}

    return thread_key, processing_results


def process_new_thread(
    url: str, reprocess: bool, message_queue, progress_update
) -> tuple:
    """Process a new thread from URL.

    Args:
        url: Thread URL
        reprocess: Force refresh flag
        message_queue: Queue for progress messages
        progress_update: Progress callback function

    Returns:
        Tuple of (thread_key, processing_results)
    """
    normalized_url = normalize_url(url)
    message_queue.put(f'Processing thread from URL: {normalized_url}\n')
    message_queue.put('This may take several minutes for large threads...\n\n')

    # Process the thread
    thread_key, processing_results = thread_processor.process_thread(
        normalized_url, force_refresh=reprocess, progress_callback=progress_update
    )

    message_queue.put(f'Thread processed: {thread_key}\n')
    message_queue.put(f'Posts processed: {processing_results.get("posts_count", 0)}\n')

    # Show analytics preview if available
    analytics_summary = processing_results.get('analytics_summary', {})
    if analytics_summary:
        metadata = analytics_summary.get('metadata', {})
        participants = analytics_summary.get('participants', {})

        message_queue.put(f'Participants: {participants.get("total_participants", "Unknown")}\n')
        message_queue.put(f'Pages: {metadata.get("total_pages", "Unknown")}\n')

        # Find most active author from participants data
        authors = participants.get('authors', {})
        if authors:
            most_active_author = max(authors.items(), key=lambda x: x[1].get('post_count', 0))
            author_name, author_data = most_active_author
            post_count = author_data.get('post_count', 0)
            if post_count > 0:
                message_queue.put(f'Most active: {author_name} ({post_count} posts)\n')

    message_queue.put('\n' + '=' * 50 + '\n\n')

    return thread_key, processing_results


def validate_thread_key(thread_key: str) -> bool:
    """Enhanced validation for thread key security.

    Args:
        thread_key: Thread key to validate

    Returns:
        True if valid, False otherwise
    """
    from utils.security import validate_thread_key as security_validate

    return security_validate(thread_key)


# -------------------- Flask Routes --------------------
@app.route('/')
def index():
    """Main application page."""
    try:
        threads = list_available_threads()
        logger.info(f'Rendering index with {len(threads)} available threads')
        return render_template('index.html', threads=threads)
    except Exception as e:
        logger.error(f'Error rendering index: {e}')
        return render_template('index.html', threads=[])


@app.route('/ask', methods=['POST'])
def ask():
    """Main query processing endpoint."""
    request_start = time.time()

    try:
        # Parse request data
        data = request.get_json()
        if not data:
            return Response(
                'Error: No JSON data provided', status=400, mimetype='text/plain'
            )

        prompt = data.get('prompt', '').strip()
        url = data.get('url', '').strip()
        existing_thread = data.get('existing_thread', '').strip()
        reprocess = data.get(
            'reprocess', False
        )  # Changed from "refresh" to "reprocess"

        # Validation using helper function
        validation_error = validate_request_parameters(prompt, url, existing_thread)
        if validation_error:
            return Response(
                f'Error: {validation_error}', status=400, mimetype='text/plain'
            )

        # When using existing thread, URL should not be provided
        if existing_thread and url:
            return Response(
                'Error: Cannot provide URL when using existing thread. Delete and recreate thread to use new URL.',
                status=400,
                mimetype='text/plain',
            )

        # When creating new thread, reprocess should not be provided
        if url and reprocess:
            return Response(
                'Error: Cannot reprocess when creating new thread from URL.',
                status=400,
                mimetype='text/plain',
            )

        # Validate existing thread key if provided
        if existing_thread and not validate_thread_key(existing_thread):
            return Response(
                'Error: Invalid thread key', status=400, mimetype='text/plain'
            )

        logger.info(
            f"Processing query: '{prompt[:MAX_LOGGED_PROMPT_LENGTH]}...' {'(reprocess)' if reprocess else ''}"
        )

        def generate_response():
            # This will be our main response generator
            import queue
            import threading

            # Create a queue for progress messages
            message_queue = queue.Queue()
            processing_complete = threading.Event()

            # Progress callback to send updates via queue
            def progress_update(message):
                message_queue.put(f'PROGRESS: {message}\n')

            def process_thread_async():
                try:
                    # Do the actual thread processing in background
                    nonlocal thread_key, processing_results

                    if existing_thread:
                        thread_key = existing_thread

                        # Check if thread exists
                        thread_dir = get_thread_dir(thread_key)
                        if not os.path.exists(thread_dir):
                            message_queue.put(
                                f"Error: Thread '{thread_key}' not found. Please delete and recreate with URL.\n"
                            )
                            return

                        # Handle reprocess request
                        if reprocess:
                            message_queue.put(
                                f'Reprocessing existing thread: {thread_key}\n'
                            )
                            message_queue.put(
                                'Re-parsing HTML files and rebuilding indexes...\n\n'
                            )

                            # Reprocess existing thread (no re-download)
                            thread_key, processing_results = (
                                thread_processor.reprocess_existing_thread(
                                    thread_key, progress_update
                                )
                            )

                            message_queue.put('Thread reprocessed successfully!\n')
                            message_queue.put(
                                f'Posts processed: {processing_results.get("posts_count", 0)}\n'
                            )
                        else:
                            # Use existing thread as-is
                            message_queue.put(f'Using existing thread: {thread_key}\n')

                            # Load processing results
                            processing_results = {
                                'thread_key': thread_key,
                                'from_cache': True,
                            }

                    else:
                        # Process new thread from URL (download + process)
                        normalized_url = normalize_url(url)
                        message_queue.put(
                            f'Creating new thread from: {normalized_url}\n'
                        )
                        message_queue.put('Downloading and processing all pages...\n\n')

                        # Process the thread (includes downloading)
                        thread_key, processing_results = (
                            thread_processor.process_thread(
                                normalized_url,
                                force_refresh=False,
                                progress_callback=progress_update,
                            )
                        )

                        message_queue.put(f'Thread processed: {thread_key}\n')
                        message_queue.put(
                            f'Posts processed: {processing_results.get("posts_count", 0)}\n'
                        )

                        # Show analytics preview if available
                        analytics_summary = processing_results.get(
                            'analytics_summary', {}
                        )
                        if analytics_summary:
                            metadata = analytics_summary.get('metadata', {})
                            participants = analytics_summary.get('participants', {})

                            message_queue.put(
                                f'Participants: {participants.get("total_participants", "Unknown")}\n'
                            )
                            message_queue.put(
                                f'Pages: {metadata.get("total_pages", "Unknown")}\n'
                            )

                            # Find most active author from participants data
                            authors = participants.get('authors', {})
                            if authors:
                                most_active_author = max(authors.items(), key=lambda x: x[1].get('post_count', 0))
                                author_name, author_data = most_active_author
                                post_count = author_data.get('post_count', 0)
                                if post_count > 0:
                                    message_queue.put(f'Most active: {author_name} ({post_count} posts)\n')

                        message_queue.put('\n' + '=' * 50 + '\n\n')

                except Exception as e:
                    message_queue.put(f'\n\nError: {str(e)}\n')
                finally:
                    processing_complete.set()

            # Start background processing
            thread_key = None
            processing_results = None

            processing_thread = threading.Thread(target=process_thread_async)
            processing_thread.start()

            try:
                # Yield messages from queue as they arrive
                while not processing_complete.is_set() or not message_queue.empty():
                    try:
                        # Get message with timeout to allow checking if processing is complete
                        message = message_queue.get(timeout=0.1)
                        yield message
                    except queue.Empty:
                        continue

                # Wait for processing thread to complete
                processing_thread.join()

                # Check if thread processing was successful
                if not thread_key:
                    yield 'Error: Thread processing failed.\n'
                    return

                # Step 2: Process the query
                yield 'Analyzing query and searching thread...\n\n'
                query_processor = get_query_processor(thread_key)

                # Get streaming response
                query_results = query_processor.process_query(prompt, stream=True)

                if 'error' in query_results:
                    yield f'Error processing query: {query_results["error"]}\n'
                    return

                # Show query analysis if available
                query_analysis = query_results.get('analysis', {})
                if query_analysis:
                    if query_analysis.get('is_vague'):
                        yield 'Detected broad/vague query - providing comprehensive overview.\n'

                    analytical_intent = query_analysis.get('analytical_intent', [])
                    if analytical_intent:
                        yield f'Analytical focus: {", ".join(analytical_intent)}\n'

                    context_hints = query_analysis.get('context_hints', [])
                    if context_hints:
                        yield f'Context: {context_hints[0]}\n'

                    yield '\n'

                # Stream the LLM response
                response_stream = query_results.get('response_stream')
                if response_stream:
                    total_chars = 0
                    for chunk in response_stream:
                        if chunk:
                            total_chars += len(chunk)
                            yield chunk

                    # Log completion stats with better context for analytical queries
                    total_time = time.time() - request_start
                    context_posts = query_results.get('context_posts', 0)
                    query_type = query_results.get('query_type', 'unknown')

                    # Add question to history after successful completion
                    question_history = get_question_history()
                    question_history.add_question(thread_key, prompt)

                    # For analytical queries, show total posts analyzed instead of context posts
                    if query_type == 'analytical':
                        analytical_result = query_results.get('analytical_result', {})
                        posts_analyzed = analytical_result.get('thread_stats', {}).get(
                            'total_posts', context_posts
                        )
                        logger.info(
                            f'Query completed in {total_time:.1f}s: '
                            f'posts_analyzed={posts_analyzed}, chars={total_chars}, type={query_type}'
                        )
                    else:
                        logger.info(
                            f'Query completed in {total_time:.1f}s: '
                            f'posts={context_posts}, chars={total_chars}, type={query_type}'
                        )
                else:
                    yield 'Error: No response generated from query processor.\n'

            except Exception as e:
                logger.error(f'Error in response generation: {e}')
                yield f'\n\nError: {str(e)}\n'

        return Response(generate_response(), mimetype='text/plain')

    except Exception as e:
        total_time = time.time() - request_start
        logger.error(f'Critical error in ask route after {total_time:.1f}s: {e}')
        return Response(f'Error: {str(e)}', status=500, mimetype='text/plain')


@app.route('/delete_thread', methods=['POST'])
def delete_thread():
    """Delete a thread and its data."""
    try:
        data = request.get_json()
        if not data:
            return 'Error: No JSON data provided', 400

        thread_key = data.get('thread_key', '').strip()

        if not thread_key:
            return 'Error: thread_key is required', 400

        if not validate_thread_key(thread_key):
            return 'Error: Invalid thread key', 400

        # Delete the thread
        success = thread_processor.delete_thread(thread_key)

        if success:
            # Clear from query processor cache
            query_processor_cache.clear()  # Clear entire cache as we don't have individual removal
            
            # Clear question history for this thread
            question_history = get_question_history()
            question_history.clear_thread_history(thread_key)

            logger.info(f'Deleted thread: {thread_key}')
            return f"Thread '{thread_key}' deleted successfully"
        else:
            return 'Error: Thread not found', 404

    except Exception as e:
        logger.error(f'Error deleting thread: {e}')
        return f'Error: {str(e)}', 500


@app.route('/threads', methods=['GET'])
def list_threads():
    """Get list of available threads with their metadata."""
    try:
        thread_summaries = thread_processor.list_processed_threads()
        return jsonify({'threads': thread_summaries, 'count': len(thread_summaries)})
    except Exception as e:
        logger.error(f'Error listing threads: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/thread/<thread_key>/summary', methods=['GET'])
def get_thread_summary(thread_key: str):
    """Get detailed summary for a specific thread."""
    try:
        if not validate_thread_key(thread_key):
            return jsonify({'error': 'Invalid thread key'}), 400

        # Load thread summary from the generated file
        thread_dir = get_thread_dir(thread_key)
        if not os.path.exists(thread_dir):
            return jsonify({'error': 'Thread not found'}), 404

        # Check for the new comprehensive summary file
        summary_file = os.path.join(thread_dir, 'thread_summary.json')
        if os.path.exists(summary_file):
            comprehensive_summary = safe_read_json(summary_file)
            if comprehensive_summary:
                return jsonify(comprehensive_summary)

        # Fallback to thread processor method for backwards compatibility
        summary = thread_processor.get_thread_summary(thread_key)

        if summary:
            return jsonify(summary)
        else:
            return jsonify({'error': 'Thread summary not available'}), 404

    except Exception as e:
        logger.error(f'Error getting thread summary: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/thread/<thread_key>/analytics', methods=['GET'])
def get_thread_analytics(thread_key: str):
    """Get detailed analytics for a specific thread."""
    try:
        if not validate_thread_key(thread_key):
            return jsonify({'error': 'Invalid thread key'}), 400

        thread_dir = get_thread_dir(thread_key)
        analyzer = ThreadAnalyzer(thread_dir)

        # Get full analytics
        analytics = safe_read_json(f'{thread_dir}/thread_analytics.json')
        if analytics:
            return jsonify(analytics)
        else:
            return jsonify({'error': 'Analytics not found'}), 404

    except Exception as e:
        logger.error(f'Error getting thread analytics: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/thread/<thread_key>/summary', methods=['POST'])
def generate_thread_summary(thread_key: str):
    """Generate an advanced LLM-powered summary for a specific thread."""
    try:
        if not validate_thread_key(thread_key):
            return jsonify({'error': 'Invalid thread key'}), 400

        data = request.get_json() or {}
        max_posts = data.get('max_posts', 15)

        # Validate max_posts parameter
        if not isinstance(max_posts, int) or max_posts < 5 or max_posts > 25:
            return jsonify(
                {'error': 'max_posts must be an integer between 5 and 25'}
            ), 400

        thread_dir = get_thread_dir(thread_key)

        # Check if thread exists
        import os

        if not os.path.exists(thread_dir):
            return jsonify({'error': 'Thread not found'}), 404

        # Import here to avoid circular imports
        from analytics.thread_summarizer import ThreadSummarizer

        logger.info(
            f'Generating summary for thread {thread_key} using {max_posts} posts'
        )

        # Generate the summary
        summarizer = ThreadSummarizer()
        summary_data = summarizer.generate_summary(thread_dir, max_posts)

        # Add thread key to response
        summary_data['thread_key'] = thread_key

        logger.info(f'Summary generated successfully for thread {thread_key}')
        return jsonify(summary_data)

    except ValueError as e:
        logger.error(f'Validation error generating summary for {thread_key}: {e}')
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f'Error generating summary for {thread_key}: {e}')
        return jsonify({'error': f'Failed to generate summary: {str(e)}'}), 500


@app.route('/search/<thread_key>', methods=['POST'])
def search_thread(thread_key: str):
    """Search within a specific thread without LLM processing."""
    try:
        if not validate_thread_key(thread_key):
            return jsonify({'error': 'Invalid thread key'}), 400

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        query = data.get('query', '').strip()
        top_k = data.get('top_k', 10)

        if not query:
            return jsonify({'error': 'Query is required'}), 400

        # Get query processor and perform search
        query_processor = get_query_processor(thread_key)
        search_results, search_metadata = query_processor.search_engine.search(
            query, top_k=top_k
        )

        return jsonify(
            {
                'results': search_results,
                'metadata': search_metadata,
                'query': query,
                'thread_key': thread_key,
            }
        )

    except Exception as e:
        logger.error(f'Error searching thread: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/thread/<thread_key>/questions', methods=['GET'])
def get_thread_questions(thread_key: str):
    """Get question history for a specific thread."""
    try:
        if not validate_thread_key(thread_key):
            return jsonify({'error': 'Invalid thread key'}), 400

        question_history = get_question_history()
        questions = question_history.get_questions(thread_key)
        
        return jsonify({
            'thread_key': thread_key,
            'questions': questions,
            'count': len(questions)
        })

    except Exception as e:
        logger.error(f'Error getting thread questions: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/thread/<thread_key>/questions', methods=['DELETE'])
def clear_thread_questions(thread_key: str):
    """Clear question history for a specific thread."""
    try:
        if not validate_thread_key(thread_key):
            return jsonify({'error': 'Invalid thread key'}), 400

        question_history = get_question_history()
        question_history.clear_thread_history(thread_key)
        
        return jsonify({
            'thread_key': thread_key,
            'message': 'Question history cleared successfully'
        })

    except Exception as e:
        logger.error(f'Error clearing thread questions: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health_check():
    """Health check endpoint."""
    try:
        threads = list_available_threads()

        # Get basic stats
        stats = thread_processor.get_stats() if thread_processor else {}

        return jsonify(
            {
                'status': 'healthy',
                'version': '2.0-optimized',
                'features': FEATURES,
                'threads_available': len(threads),
                'processing_stats': stats,
                'memory_status': get_memory_status(),
                'config': {
                    'ollama_url': OLLAMA_BASE_URL,
                    'chat_model': OLLAMA_CHAT_MODEL,
                    'embed_model': OLLAMA_EMBED_MODEL,
                    'threads_dir': THREADS_DIR,
                },
            }
        )

    except Exception as e:
        logger.error(f'Health check error: {e}')
        return jsonify({'status': 'error', 'error': str(e)}), 500


@app.route('/stats')
def get_stats():
    """Get detailed application statistics."""
    try:
        # Get processing stats
        processing_stats = thread_processor.get_stats()

        # Get query processor cache stats
        query_stats = {
            'cache_size': query_processor_cache.size(),
            'max_cache_size': query_processor_cache._max_size,
        }

        return jsonify(
            {
                'processing': processing_stats,
                'queries': query_stats,
                'active_processors': query_processor_cache.size(),
                'uptime_hours': (time.time() - app_start_time) / 3600,
            }
        )

    except Exception as e:
        logger.error(f'Error getting stats: {e}')
        return jsonify({'error': str(e)}), 500


# -------------------- Topic Index API Endpoints --------------------
@app.route('/thread/<thread_key>/topics', methods=['GET'])
def get_thread_topics(thread_key: str):
    """Get topic index for a specific thread."""
    try:
        from utils.topic_cache import TopicIndexCache
        
        topic_cache = TopicIndexCache()
        
        if not topic_cache.has_topic_index(thread_key):
            return jsonify({'error': 'Topic index not found for this thread'}), 404
        
        topic_index = topic_cache.load_topic_index(thread_key)
        if not topic_index:
            return jsonify({'error': 'Failed to load topic index'}), 500
        
        return jsonify(topic_index)
        
    except Exception as e:
        logger.error(f'Error getting topics for thread {thread_key}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/thread/<thread_key>/topics/<topic_id>', methods=['GET'])
def get_thread_topic_matches(thread_key: str, topic_id: str):
    """Get specific topic matches for a thread."""
    try:
        from utils.topic_cache import TopicIndexCache
        
        topic_cache = TopicIndexCache()
        matches = topic_cache.get_topic_matches_for_thread(thread_key, topic_id)
        
        if not matches:
            return jsonify({'matches': [], 'message': 'No matches found for this topic'}), 200
        
        return jsonify({
            'topic_id': topic_id,
            'thread_key': thread_key,
            'match_count': len(matches),
            'matches': matches
        })
        
    except Exception as e:
        logger.error(f'Error getting topic {topic_id} matches for thread {thread_key}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/thread/<thread_key>/topics/summary', methods=['GET'])
def get_thread_topic_summary(thread_key: str):
    """Get topic summary for a thread."""
    try:
        from utils.topic_cache import TopicIndexCache
        
        topic_cache = TopicIndexCache()
        summary = topic_cache.get_thread_topic_summary(thread_key)
        
        if not summary:
            return jsonify({'error': 'Topic summary not found for this thread'}), 404
        
        return jsonify(summary)
        
    except Exception as e:
        logger.error(f'Error getting topic summary for thread {thread_key}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/topics/search/<topic_id>', methods=['GET'])
def search_topic_across_threads(topic_id: str):
    """Search for a topic across all threads."""
    try:
        from utils.topic_cache import TopicIndexCache
        
        topic_cache = TopicIndexCache()
        limit = request.args.get('limit', 50, type=int)
        
        matches = topic_cache.search_topics_across_threads(topic_id, limit)
        
        return jsonify({
            'topic_id': topic_id,
            'total_matches': len(matches),
            'limit': limit,
            'matches': matches
        })
        
    except Exception as e:
        logger.error(f'Error searching topic {topic_id} across threads: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/topics/available', methods=['GET'])
def get_available_topics():
    """Get all available topic definitions."""
    try:
        from analytics.topic_indexer import TopicIndexer
        
        topic_indexer = TopicIndexer()
        topics = topic_indexer.get_available_topics()
        
        return jsonify({
            'total_topics': len(topics),
            'topics': topics
        })
        
    except Exception as e:
        logger.error(f'Error getting available topics: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/topics/cache/stats', methods=['GET'])
def get_topic_cache_stats():
    """Get topic cache statistics."""
    try:
        from utils.topic_cache import TopicIndexCache
        
        topic_cache = TopicIndexCache()
        stats = topic_cache.get_cache_stats()
        
        return jsonify(stats)
        
    except Exception as e:
        logger.error(f'Error getting topic cache stats: {e}')
        return jsonify({'error': str(e)}), 500


# -------------------- Error Handlers --------------------
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def internal_error(error):
    logger.error(f'Internal server error: {error}')
    return jsonify({'error': 'Internal server error'}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    """Production-ready exception handler with detailed logging."""
    import traceback

    error_id = str(time.time())  # Simple error ID for tracking
    logger.error(f'Unhandled exception [{error_id}]: {e}')
    logger.error(f'Traceback [{error_id}]: {traceback.format_exc()}')

    # Don't expose internal errors in production
    if app.debug:
        return jsonify({'error': str(e), 'error_id': error_id}), 500
    else:
        return jsonify(
            {'error': 'An unexpected error occurred', 'error_id': error_id}
        ), 500


# -------------------- Application Startup --------------------
app_start_time = time.time()

if __name__ == '__main__':
    try:
        # Ensure required directories exist
        os.makedirs(BASE_TMP_DIR, exist_ok=True)
        os.makedirs(THREADS_DIR, exist_ok=True)

        # Create log file with proper error handling
        log_file = os.path.join(BASE_TMP_DIR, 'app.log')
        try:
            with open(log_file, 'a') as f:
                f.write(
                    f'\n--- Forum Wisdom Miner v2.0 (M1 Optimized) started at {time.strftime("%Y-%m-%d %H:%M:%S")} ---\n'
                )
        except Exception as e:
            logger.warning(f'Could not write to log file: {e}')

        # Log startup information with M1 optimizations
        logger.info('=' * 60)
        logger.info('Starting Forum Wisdom Miner v2.0 (M1 MacBook Air Optimized)')
        logger.info('=' * 60)
        logger.info(f'Base directory: {BASE_TMP_DIR}')
        logger.info(f'Threads directory: {THREADS_DIR}')
        logger.info(f'Ollama URL: {OLLAMA_BASE_URL}')
        logger.info(f'Chat model: {OLLAMA_CHAT_MODEL}')
        logger.info(f'Embedding model: {OLLAMA_EMBED_MODEL}')
        logger.info(
            f'Features enabled: {", ".join(f for f, enabled in FEATURES.items() if enabled)}'
        )
        logger.info(
            f'M1 Optimizations: MAX_WORKERS={MAX_WORKERS}, BATCH_SIZE={EMBEDDING_BATCH_SIZE}'
        )

        # Check existing threads with error handling
        try:
            existing_threads = list_available_threads()
            logger.info(f'Found {len(existing_threads)} existing threads')
        except Exception as e:
            logger.error(f'Error checking existing threads: {e}')
            existing_threads = []

        logger.info('Application ready!')
        logger.info('=' * 60)

        # Docker-friendly Flask configuration
        debug_mode = os.environ.get('FLASK_ENV') == 'development'
        port = int(os.environ.get('PORT', 8080))

        logger.info(f'Starting Flask server on 0.0.0.0:{port} (debug={debug_mode})')

        # Start the Flask application with Docker-compatible settings
        app.run(
            host='0.0.0.0',
            port=port,
            debug=debug_mode,
            threaded=True,
            use_reloader=debug_mode,  # Enable hot reload only in development
        )

    except Exception as e:
        logger.critical(f'Failed to start application: {e}')
        raise
