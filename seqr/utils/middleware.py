from django.core.exceptions import PermissionDenied, ObjectDoesNotExist
from django.utils.deprecation import MiddlewareMixin
import logging
import traceback

from seqr.views.utils.json_utils import create_json_response
from settings import DEBUG

logger = logging.getLogger()


EXCEPTION_ERROR_MAP = {
    PermissionDenied: 403,
    ObjectDoesNotExist: 404,
}


class JsonErrorMiddleware(MiddlewareMixin):

    @staticmethod
    def process_exception(request, exception):
        if request.path.startswith('/api'):
            exception_json = {'message': str(exception)}
            traceback_message = traceback.format_exc()
            logger.error(traceback_message)
            if DEBUG:
                exception_json['traceback'] = traceback_message.split('\n')
            return create_json_response(
                exception_json,
                status=next((code for exc, code in EXCEPTION_ERROR_MAP.items() if isinstance(exception, exc)), 500))
        return None