import numpy
import StringIO


_CL_MODE = False  # "pyopencl" in __name__


if _CL_MODE:
    # THIS IS NOT FINISHED
    import pyopencl as cl
    import pyopencl.array as cl_array

    #    import pyopencl._mymako as mako
    from pyopencl._cluda import CLUDA_PREAMBLE
    from pyopencl.tools import dtype_to_ctype

    # TODO: use mako to get rid of the %if
    CLUDA_PREAMBLE = CLUDA_PREAMBLE[:455]
    CLUDA_PREAMBLE += """
#define LDIM_0 get_local_id(0)
#define LDIM_1 get_local_id(1)
#define LDIM_2 get_local_id(2)

#define GDIM_0 get_global_id(0)
#define GDIM_1 get_global_id(1)
#define GDIM_2 get_global_id(2)
 """
    # TODO, reuse the same context as the use used to create the memory.
    ctx = cl.create_some_context()
    queue = cl.CommandQueue(ctx)
else:
    import pycuda.autoinit
    import pycuda.driver as driver

    #    import pycuda._mymako as mako
    from pycuda._cluda import CLUDA_PREAMBLE
    from pycuda.compiler import SourceModule
    from pycuda.tools import dtype_to_ctype
    CLUDA_PREAMBLE += """
#define LDIM_0 blockDim.x
#define LDIM_1 blockDim.y
#define LDIM_2 blockDim.z

#define GDIM_0 gridDim.x
#define GDIM_1 gridDim.y
#define GDIM_2 gridDim.z
 """

import logging

import theano
from theano import Apply, scalar
from theano.sandbox.cuda import CudaNdarrayType
from theano.tensor import TensorType


_logger_name = 'compyte.gen_reduction'
_logger = logging.getLogger(_logger_name)
_logger.setLevel(logging.INFO)
_logger.addHandler(logging.StreamHandler())  # TO REMOVE


def warning(*msg):
    _logger.warning(_logger_name + 'WARNING: ' + ' '.join(str(m) for m in msg))


def info(*msg):
    _logger.info(_logger_name + 'INFO: ' + ' '.join(str(m) for m in msg))


def debug(*msg):
    _logger.debug(_logger_name + 'DEBUG: ' + ' '.join(str(m) for m in msg))


import pygpu_ndarray as gpu_ndarray


class GpuSum:
    """GpuSum is a Reduction along some dimensions by summation.

    The dimensions along which to sum is specified by the
    `reduce_mask` that you pass to the constructor.  The `reduce_mask`
    is a tuple of booleans (actually integers 0 or 1) that specify for
    each input dimension, whether to reduce it (1) or not (0).

    For example:

      - reduce_mask == (1,) sums a vector to a scalar

      - reduce_mask == (1,0) computes the sum of each column in a matrix

      - reduce_mask == (0,1) computes the sum of each row in a matrix

      - reduce_mask == (1,1,1) computes the sum of all elements in a
        3-tensor.

    :note: any reduce_mask of all zeros is a sort of 'copy', and may
           be removed during graph optimization

    """
    def __init__(self, reduce_mask, dtype):
        self.reduce_mask = tuple(reduce_mask)
        # input, output and accumulator dtype
        self.dtype = dtype_to_ctype(dtype)

    def __eq__(self, other):
        return (type(self) == type(other) and
                self.reduce_mask == other.reduce_mask)

    def __hash__(self):
        return hash(type(self)) ^ hash(self.reduce_mask)

    def __str__(self):
        return "GpuSum{%s}" % ','.join(str(i) for i in self.reduce_mask)

    def make_node(self, x):
        if (x.type.ndim != len(self.reduce_mask)):
            raise TypeError("x must have rank %i" % len(self.reduce_mask))
        o_broadcast = [x.type.broadcastable[i]
                       for i in range(x.type.ndim) if not self.reduce_mask[i]]
        return Apply(self, [x], [CudaNdarrayType(o_broadcast)()])

    def perform(self, node, inp, out):
        x, = inp
        z, = out
        z[0] = x.reduce_sum(self.reduce_mask)

    def c_code(self, node, name, inp, out, sub):
        x, = inp
        z, = out

        nd_in = node.inputs[0].type.ndim
        nd_out = node.outputs[0].type.ndim

        assert nd_in - nd_out == sum(self.reduce_mask)

        sio = StringIO.StringIO()
        fail = sub['fail']

        #check input
        print("""
        if (%(x)s->nd != %(nd_in)s)
        {
            PyErr_Format(PyExc_TypeError,
                         "required nd=%(nd_in)s, got nd=%%i", %(x)s->nd);
            %(fail)s;
        }
        """ % locals(), file=sio)

        #
        # alloc an output if we need one
        #

        # check the basics of out output
        print("""
        if (  !%(z)s
           || (%(z)s->nd != %(nd_out)s)
        """ % locals(), file=sio)

        #ensure that the output has the right non-reduced dimensions
        j = 0
        for i in range(nd_in):
            if not self.reduce_mask[i]:
                print((" || (CudaNdarray_HOST_DIMS(%(z)s)[%(j)s] !="
                               "CudaNdarray_HOST_DIMS(%(x)s)[%(i)s]) " %
                               locals()), file=sio)
                j += 1

        print("""
           )
        {
            """ % locals(), file=sio)
        print("int new_dims[%(nd_out)s]; " % locals(), file=sio)

        j = 0
        for i in range(nd_in):
            if not self.reduce_mask[i]:
                print(('new_dims[%(j)s] = CudaNdarray_HOST_DIMS'
                               '(%(x)s)[%(i)s];' % locals()), file=sio)
                j += 1

        print("""
            Py_XDECREF(%(z)s);
            %(z)s = (CudaNdarray*) CudaNdarray_NewDims(%(nd_out)s, new_dims);
            if (NULL == %(z)s)
            {
                PyErr_Format(PyExc_RuntimeError, "Failed to allocate output");
                %(fail)s;
            }
        }
        """ % locals(), file=sio)

        # \begin bracket the reduction in a check that there is
        # actually work to do
        print("""
        if (CudaNdarray_SIZE(%(z)s))
        {
        """ % locals(), file=sio)

        #
        # Now perform the reduction
        #

        if all(i == 1 for i in self.reduce_mask):
            #check if the tensor is ccontiguous, if true, use the
            #c_c0de_reduce_ccontig code.
            #TODO: check if we are ccontiguous when we un-dimshuffle
            #TODO: if only some dims are ccontiguous, call version
            #      with less dims.

            print('if(CudaNdarray_is_c_contiguous(%(x)s)){' % locals(), file=sio)
            self.c_code_reduce_ccontig(sio, node, name, x, z, fail)
            print("}else{", file=sio)
            getattr(self, 'c_code_reduce_%s' % (''.join(
                        str(i) for i in self.reduce_mask)))(sio, node, name,
                                                            x, z, fail)
            print("}", file=sio)
        else:
            getattr(self, 'c_code_reduce_%s' % (''.join(
                        str(i) for i in self.reduce_mask)))(sio, node, name,
                                                            x, z, fail)

        # \end bracket the reduction ...
        print("""
        }
        """ % locals(), file=sio)

        return sio.getvalue()

    def _makecall(self, node, name, x, z, fail, pattern=None):
        """Return a string for making a kernel call.

            The return value looks something like:

            .. code-block:: c

                if (verbose)
                    printf("running kernel_reduce_sum_10_%(name)s\\n");
                int n_shared = sizeof(%(dtype)s) * n_threads.x;
                kernel_reduce_sum_10_%(name)s<<<n_blocks,
                                                n_threads, n_shared>>>(
                        CudaNdarray_HOST_DIMS(%(x)s)[0],
                        CudaNdarray_HOST_DIMS(%(x)s)[1],
                        CudaNdarray_DEV_DATA(%(x)s),
                        CudaNdarray_HOST_STRIDES(%(x)s)[0],
                        CudaNdarray_HOST_STRIDES(%(x)s)[1],
                        CudaNdarray_DEV_DATA(%(z)s),
                        CudaNdarray_HOST_STRIDES(%(z)s)[0]
                        );
                CNDA_THREAD_SYNC;
                if (cudaSuccess != cudaGetLastError())
                {
                    PyErr_Format(PyExc_RuntimeError, "Cuda error: ... );
                    %(fail)s;
                }
        """
        sio = StringIO.StringIO()
        if pattern is None:
            pattern = ''.join(str(c) for c in self.reduce_mask)
        ndim = len(self.reduce_mask)
        nd_out = ndim - sum(self.reduce_mask)
        print("""
            if (verbose)
                printf("running kernel_reduce_sum_%(pattern)s_%(name)s\\n");
            int n_shared = sizeof(%(dtype)s) * n_threads.x *
                           n_threads.y * n_threads.z;
            if (verbose>1)
                printf("n_threads.x=%%d, n_threads.y=%%d, n_threads.z=%%d,"
                       " nb_threads=%%d, n_blocks.x=%%d, n_blocks.y=%%d,"
                       " nb_block=%%d, n_shared=%%d\\n",
                                  n_threads.x,n_threads.y,n_threads.z,
                                  n_threads.x*n_threads.y*n_threads.z,
                                  n_blocks.x,n_blocks.y,
                                  n_blocks.x*n_blocks.y, n_shared);
            kernel_reduce_sum_%(pattern)s_%(name)s<<<n_blocks,
                                                     n_threads, n_shared>>>(
            """ % locals(), file=sio)
        for i in range(ndim):
            print("""
                    CudaNdarray_HOST_DIMS(%(x)s)[%(i)s],
            """ % locals(), file=sio)
        print("""
                    CudaNdarray_DEV_DATA(%(x)s)
            """ % locals(), file=sio)
        for i in range(ndim):
            print("""
                    ,CudaNdarray_HOST_STRIDES(%(x)s)[%(i)s]
            """ % locals(), file=sio)
        print("""
                    ,CudaNdarray_DEV_DATA(%(z)s)
            """ % locals(), file=sio)
        for i in range(nd_out):
            print("""
                    ,CudaNdarray_HOST_STRIDES(%(z)s)[%(i)s]
            """ % locals(), file=sio)
        print("""
                    );
            CNDA_THREAD_SYNC;
            cudaError_t sts = cudaGetLastError();
            if (cudaSuccess != sts)
            {
                PyErr_Format(PyExc_RuntimeError,
"Cuda error: %%s: %%s. (grid: %%i x %%i; block: %%i x %%i x %%i)\\n",
                    "kernel_reduce_sum_%(pattern)s_%(name)s",
                    cudaGetErrorString(sts),
                    n_blocks.x,
                    n_blocks.y,
                    n_threads.x,
                    n_threads.y,
                    n_threads.z);
                %(fail)s;
            }
        """ % locals(), file=sio)
        return sio.getvalue()

    def _k_decl(self, nodename,
                pattern=None, ndim=None, reduce_mask=None):
        """Return a string to declare a kernel function

        .. code-block:: c

            __global__ void kernel_reduce_sum_110_%(nodename)s(
                    const int d0,
                    const int d1,
                    const int d2,
                    const %(dtype)s *A,
                    const int sA0,
                    const int sA1,
                    const int sA2,
                    %(dtype)s * Z,
                    const int sZ0)

        """
        dtype = self.dtype
        if reduce_mask is None:
            reduce_mask = self.reduce_mask
        if ndim is None:
            ndim = len(reduce_mask)
        if pattern is None:
            pattern = ''.join(str(i) for i in reduce_mask)
        sio = StringIO.StringIO()

        print("""
            __global__ void kernel_reduce_sum_%(pattern)s_%(nodename)s(
        """ % locals(), file=sio)

        for i in range(ndim):
            print("""const int d%(i)s,""" % locals(), file=sio)

        print("""const %(dtype)s *A,""" % locals(), file=sio)

        for i in range(ndim):
            print("""const int sA%(i)s,""" % locals(), file=sio)

        print("""%(dtype)s * Z""" % locals(), file=sio)

        for i in range(ndim - sum(reduce_mask)):
            print(""", const int sZ%(i)s""" % locals(), file=sio)

        print(")", file=sio)

        return sio.getvalue()

    def _k_init(self, *args):
        dtype = self.dtype
        return """
                const int threadCount = blockDim.x * blockDim.y * blockDim.z;
                const int threadNum = threadIdx.z * blockDim.x * blockDim.y
                                      + threadIdx.y * blockDim.x + threadIdx.x;
                extern __shared__ %(dtype)s buf[];
                %(dtype)s mysum = 0.0f;

                if (warpSize != 32){ //TODO: set error code
                    Z[0] = 666;
                    return;
                }

        """ % locals()

    def _k_reduce_buf(self, z_pos):
        return """
        __syncthreads(); // some kernel do multiple reduction.
        buf[threadNum] = mysum;
        __syncthreads();

        // rest of function is handled by one warp
        if (threadNum < warpSize)
        {
            //round up all the partial sums into the first `warpSize` elements
            for (int i = threadNum + warpSize; i < threadCount; i += warpSize)
            {
                mysum += buf[i];
            }
            buf[threadNum] = mysum;
            if (threadNum < 16)
            {
                //reduce so that threadNum 0 has the sum of everything
                if(threadNum + 16 < threadCount)
                    buf[threadNum] += buf[threadNum+16];
                if(threadNum + 8 < threadCount)
                    buf[threadNum] += buf[threadNum+8];
                if(threadNum + 4 < threadCount)
                    buf[threadNum] += buf[threadNum+4];
                if(threadNum + 2 < threadCount)
                    buf[threadNum] += buf[threadNum+2];
                if(threadNum + 1 < threadCount)
                    buf[threadNum] += buf[threadNum+1];
                if (threadNum == 0)
                {
                    %(z_pos)s = buf[0];
                }
            }
        }
        """ % locals()
        return """
        __syncthreads(); // some kernel do multiple reduction.
        buf[threadNum] = mysum;
        __syncthreads();

        // rest of function is handled by one warp
        if (threadNum < warpSize)
        {
            //round up all the partial sums into the first `warpSize` elements
            for (int i = threadNum + warpSize; i < threadCount; i += warpSize)
            {
                mysum += buf[i];
            }
            buf[threadNum] = mysum;
/*Comment this optimization as it don't work on Fermi GPU.
TODO: find why it don't work or put the GPU compute capability into the version
            // no sync because only one warp is running
            if(threadCount >32)
            {
                buf[threadNum] += buf[threadNum+16];
                buf[threadNum] += buf[threadNum+8];
                buf[threadNum] += buf[threadNum+4];
                buf[threadNum] += buf[threadNum+2];
                buf[threadNum] += buf[threadNum+1];
                if (threadNum == 0)
                {
                    %(z_pos)s = buf[0];
                }

            }
            else */
            if (threadNum < 16)
            {
                //reduce so that threadNum 0 has the sum of everything
                if(threadNum + 16 < threadCount)
                    buf[threadNum] += buf[threadNum+16];
                if(threadNum + 8 < threadCount)
                    buf[threadNum] += buf[threadNum+8];
                if(threadNum + 4 < threadCount)
                    buf[threadNum] += buf[threadNum+4];
                if(threadNum + 2 < threadCount)
                    buf[threadNum] += buf[threadNum+2];
                if(threadNum + 1 < threadCount)
                    buf[threadNum] += buf[threadNum+1];
                if (threadNum == 0)
                {
                    %(z_pos)s = buf[0];
                }
            }
        }
        """ % locals()

    # Threads must be organized as: threadNum%nb_reduce correspond to
    # the same sum
    # nb_reduce<=warpSize
    def _k_reduce_buf_multiple(self, z_pos, nb_reduce):
        return """
        __syncthreads(); // some kernel do multiple reduction.
        buf[threadNum] = mysum;
        __syncthreads();

        // rest of function is handled by one warp
        if (threadNum < %(nb_reduce)s)
        {
            //round up all the partial sums into the first `nb_reduce` elements
            for (int i = threadNum + %(nb_reduce)s;
                 i < threadCount; i += %(nb_reduce)s)
            {
                mysum += buf[i];
            }
            %(z_pos)s = mysum;
        }
        """ % locals()

    def c_code_reduce_ccontig(self, sio, node, name, x, z, fail):
        print("""
        {
          if(CudaNdarray_SIZE(%(x)s)==0){
            cudaMemset(CudaNdarray_DEV_DATA(%(z)s),0,sizeof(%(dtype)s));
          }else{
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_SIZE(%(x)s),
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            dim3 n_blocks(1);
            if (verbose)
                printf("running kernel_reduce_sum_ccontig_%(name)s"
                       " n_threads.x=%%d, size=%%d, ndim=%%d\\n",
                                n_threads.x,CudaNdarray_SIZE(%(x)s),%(x)s->nd);
            int n_shared = sizeof(%(dtype)s) * n_threads.x;
            kernel_reduce_sum_ccontig_%(name)s<<<n_blocks,
                                                 n_threads, n_shared>>>(
                    CudaNdarray_SIZE(%(x)s),
                    CudaNdarray_DEV_DATA(%(x)s),
                    CudaNdarray_DEV_DATA(%(z)s));
            CNDA_THREAD_SYNC;
            cudaError_t sts = cudaGetLastError();
            if (cudaSuccess != sts)
            {
                PyErr_Format(PyExc_RuntimeError,
                    "Cuda error: %%s: %%s. (grid: %%i x %%i;"
                    " block: %%i x %%i x %%i)\\n",
                    "kernel_reduce_sum_ccontig_%(name)s",
                    cudaGetErrorString(sts),
                    n_blocks.x,
                    n_blocks.y,
                    n_threads.x,
                    n_threads.y,
                    n_threads.z);
                %(fail)s;
            }
         }
        }
        """ % locals(), file=sio)

    def c_code_reduce_1(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            dim3 n_blocks(1);
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_11(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[1],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            while (n_threads.y * n_threads.x <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
                ++n_threads.y;
            n_threads.y -= 1;
            if (n_threads.y > CudaNdarray_HOST_DIMS(%(x)s)[0])
                n_threads.y = CudaNdarray_HOST_DIMS(%(x)s)[0];

            dim3 n_blocks(1);
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_01X(self, sio, node, name, x, z, fail, N):
        """
        :param N: the number of 1 in the pattern N=1 -> 01, N=2 -> 011,
                  N=3 ->0111 Work for N=1,2,3
        """
        assert N in [1, 2, 3]
        makecall = self._makecall(node, name, x, z, fail)
        N_pattern = ''.join(['1'] * N)
        param_dim = ",".join(["CudaNdarray_HOST_DIMS(%(x)s)[%(i)s]" % locals()
                              for i in range(N + 1)])
        strides_dim = ",".join(
            ["CudaNdarray_HOST_STRIDES(%(x)s)[%(i)s]" % locals()
                                for i in range(N + 1)])
        threads_y = """
            //get as many y threads as we can fit
            while (n_threads.x * (n_threads.y+1) <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
            {
                if (n_threads.y < CudaNdarray_HOST_DIMS(%(x)s)[%(N)s-1])
                    n_threads.y += 1;
                else
                    break;
            }
""" % locals()
        threads_z = """
            //get as many z threads as we can fit
            while (n_threads.x * n_threads.y * (n_threads.z+1) <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
            {
                if (n_threads.z < CudaNdarray_HOST_DIMS(%(x)s)[%(N)s-2])
                    n_threads.z += 1;
                else
                    break;
            }
""" % locals()
        if len(self.reduce_mask) == 2:
            threads_y = ''
            threads_z = ''
        if len(self.reduce_mask) == 3:
            threads_z = ''
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[%(N)s],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            %(threads_y)s
            %(threads_z)s
            dim3 n_blocks(std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                          NUM_VECTOR_OP_BLOCKS));
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_01(self, sio, node, name, x, z, fail):
        self.c_code_reduce_01X(sio, node, name, x, z, fail, 1)

    def c_code_reduce_011(self, sio, node, name, x, z, fail):
        self.c_code_reduce_01X(sio, node, name, x, z, fail, 2)

    def c_code_reduce_0111(self, sio, node, name, x, z, fail):
        self.c_code_reduce_01X(sio, node, name, x, z, fail, 3)

    def c_code_reduce_10(self, sio, node, name, x, z, fail):
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            dim3 n_blocks(1,
                std::min(CudaNdarray_HOST_DIMS(%(x)s)[1],
                    NUM_VECTOR_OP_BLOCKS));
            if (verbose) {
              fprintf(stderr,
                "running kernel_reduce_sum_10_%(name)s n_blocks=(%%i,%%i)\\n",
                n_blocks.x,
                n_blocks.y);
            }
            assert(CudaNdarray_HOST_DIMS(%(x)s)[1] ==
                   CudaNdarray_HOST_DIMS(%(z)s)[0]);
            int n_shared = sizeof(%(dtype)s) * n_threads.x;
            kernel_reduce_sum_010_%(name)s<<<n_blocks, n_threads, n_shared>>>(
                    1,
                    CudaNdarray_HOST_DIMS(%(x)s)[0],
                    CudaNdarray_HOST_DIMS(%(x)s)[1],
                    CudaNdarray_DEV_DATA(%(x)s),
                    1,
                    CudaNdarray_HOST_STRIDES(%(x)s)[0],
                    CudaNdarray_HOST_STRIDES(%(x)s)[1],
                    CudaNdarray_DEV_DATA(%(z)s),
                    1,
                    CudaNdarray_HOST_STRIDES(%(z)s)[0]
                    );
            CNDA_THREAD_SYNC;
            cudaError_t sts = cudaGetLastError();
            if (cudaSuccess != sts)
            {
                PyErr_Format(PyExc_RuntimeError,
"Cuda error: %%s: %%s. (grid: %%i x %%i; block: %%i x %%i x %%i)\\n",
                    "kernel_reduce_sum_010_%(name)s",
                    cudaGetErrorString(sts),
                    n_blocks.x,
                    n_blocks.y,
                    n_threads.x,
                    n_threads.y,
                    n_threads.z);
                %(fail)s;
            }
        }
        """ % locals(), file=sio)

    def c_code_reduce_010(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        makecall_inner = self._makecall(node, name, x, z,
                                        fail, pattern="010_inner")
        pattern = ''.join(str(i) for i in self.reduce_mask)
        print("""
        {

 // if the alternative is less buggy, consider not using this branch
            if (1)
            {
                // If there are a lot of summations to do, then we can use
                // simple parallelization -  use each thread to do one sum.
                // we might as well launch blocks of 32 threads because that's
                // the warp size. we could schedule more threads if we were
                // maxing out the gridsize below, but the gridsize is way more
                // than the physical hardware and I think 32 threads
                // on a huge grid is enough to fully use the hardware.
                dim3 n_threads(32,1,1);

                // We kindof reshape the input implicitly to something 4D:
                //  the shape A,B,C    ->   A, B, D, E
                //  where C <= D*E < C+32
                //  where E==32

                int A = CudaNdarray_HOST_DIMS(%(x)s)[0];
                int B = CudaNdarray_HOST_DIMS(%(x)s)[1];
                int C = CudaNdarray_HOST_DIMS(%(x)s)[2];
                int D = C/32;
                if (32*D < C) D+= 1;
                assert ((C <= 32*D) && (32*D < C+32));

                // The gridsize would ideally be (A, D).  But we do the
                // following logic to make sure we don't ask for a grid that
                // is too big.
                dim3 n_blocks(A,D);
                if (n_blocks.x > NUM_VECTOR_OP_BLOCKS)
                     n_blocks.x = NUM_VECTOR_OP_BLOCKS;
                if (n_blocks.x*n_blocks.y > NUM_VECTOR_OP_BLOCKS)
                    n_blocks.y = NUM_VECTOR_OP_BLOCKS/n_blocks.x;
                int n_shared = 0;
                kernel_reduce_sum_010_AD_%(name)s<<<n_blocks,
                                                    n_threads, n_shared>>>(
                        A,B,C,D,
                        CudaNdarray_DEV_DATA(%(x)s),
                        CudaNdarray_HOST_STRIDES(%(x)s)[0],
                        CudaNdarray_HOST_STRIDES(%(x)s)[1],
                        CudaNdarray_HOST_STRIDES(%(x)s)[2],
                        CudaNdarray_DEV_DATA(%(z)s),
                        CudaNdarray_HOST_STRIDES(%(z)s)[0],
                        CudaNdarray_HOST_STRIDES(%(z)s)[1]
                        );
                CNDA_THREAD_SYNC;
                cudaError_t sts = cudaGetLastError();
                if (cudaSuccess != sts)
                {
                    PyErr_Format(PyExc_RuntimeError,
"Cuda error: %%s: %%s. (grid: %%i x %%i; block: %%i x %%i x %%i)\\n",
                        "kernel_reduce_sum_010_%(name)s",
                        cudaGetErrorString(sts),
                        n_blocks.x,
                        n_blocks.y,
                        n_threads.x,
                        n_threads.y,
                        n_threads.z);
                    %(fail)s;
                }
            }
            else
            {
                int verbose = 2;

                  dim3 n_threads(std::min(32,CudaNdarray_HOST_DIMS(%(x)s)[2]));
                  while((n_threads.x*(n_threads.y+1) <=
                        NUM_VECTOR_OP_THREADS_PER_BLOCK)
                        && (n_threads.y<CudaNdarray_HOST_DIMS(%(x)s)[1])){
                      n_threads.y++;
                  }

                  dim3 n_blocks(std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                                (int)NUM_VECTOR_OP_BLOCKS));
                  n_blocks.y = std::min(
                      ceil_intdiv(CudaNdarray_HOST_DIMS(%(x)s)[2],
                                  (int)n_threads.x),
                      (int)(NUM_VECTOR_OP_BLOCKS / n_blocks.x)
                      );
                if(std::min(std::min(CudaNdarray_HOST_STRIDES(%(x)s)[0],
                                     CudaNdarray_HOST_STRIDES(%(x)s)[1]),
                            CudaNdarray_HOST_STRIDES(%(x)s)[2])
                   ==CudaNdarray_HOST_STRIDES(%(x)s)[2]
                  && n_blocks.y == ceil_intdiv(CudaNdarray_HOST_DIMS(%(x)s)[2],
                                              (int)n_threads.x)){
                  if(verbose>1)
                    printf("n_block.x.1=%%d, n_block.x.2=%%d,"
                           " n_block.y.1=%%d, n_block.y.2=%%d,\\n",
                           CudaNdarray_HOST_DIMS(%(x)s)[0],
                           NUM_VECTOR_OP_BLOCKS,
                           ceil_intdiv(CudaNdarray_HOST_DIMS(%(x)s)[2],
                                      (int)n_threads.x),
                           (int)(NUM_VECTOR_OP_BLOCKS / n_blocks.x));
                  assert(n_threads.x<=32);
                  %(makecall_inner)s
                }else{
                  n_threads.x = std::min(CudaNdarray_HOST_DIMS(%(x)s)[1],
                                  (int)NUM_VECTOR_OP_THREADS_PER_BLOCK);
                  n_blocks.x = std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                                        (int)NUM_VECTOR_OP_BLOCKS);
                  n_blocks.y = std::min(
                      CudaNdarray_HOST_DIMS(%(x)s)[2],
                      (int)(NUM_VECTOR_OP_BLOCKS / n_blocks.x)
                      );
                  %(makecall)s
                }
                CNDA_THREAD_SYNC;
                cudaError_t sts = cudaGetLastError();
                if (cudaSuccess != sts)
                {
                    PyErr_Format(PyExc_RuntimeError,
"Cuda error: %%s: %%s. (grid: %%i x %%i; block: %%i x %%i x %%i)\\n",
                        "kernel_reduce_sum_%(pattern)s_%(name)s",
                        cudaGetErrorString(sts),
                        n_blocks.x,
                        n_blocks.y,
                        n_threads.x,
                        n_threads.y,
                        n_threads.z);
                    %(fail)s;
                }
            }
        }
        """ % locals(), file=sio)

    def c_code_reduce_0101(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[3],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            while (n_threads.x * n_threads.y <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
            {
                if (n_threads.y > CudaNdarray_HOST_DIMS(%(x)s)[1]) break;
                n_threads.y += 1;
            }
            n_threads.y -= 1;
            dim3 n_blocks(CudaNdarray_HOST_DIMS(%(x)s)[0],
                          CudaNdarray_HOST_DIMS(%(x)s)[2]);
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_100(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        # use threadIdx.x for i0
        # use blockIdx.x for i1
        # use blockIdx.y for i2
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            dim3 n_blocks(CudaNdarray_HOST_DIMS(%(x)s)[1]);
            while (n_blocks.x * (n_blocks.y+1) <= NUM_VECTOR_OP_BLOCKS
                   && n_blocks.y <= CudaNdarray_HOST_DIMS(%(x)s)[2])
            {
                n_blocks.y += 1;
            }
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_110(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[1],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            while (n_threads.x*n_threads.y <= NUM_VECTOR_OP_THREADS_PER_BLOCK)
            {
                if (n_threads.y > CudaNdarray_HOST_DIMS(%(x)s)[0])
                    break;
                n_threads.y += 1;
            }
            n_threads.y -= 1;

            dim3 n_blocks(CudaNdarray_HOST_DIMS(%(x)s)[2]);
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_001(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[2],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            dim3 n_blocks(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                        NUM_VECTOR_OP_BLOCKS));
            while (n_blocks.x * n_blocks.y <= NUM_VECTOR_OP_BLOCKS)
            {
                if (n_blocks.y > CudaNdarray_HOST_DIMS(%(x)s)[1])
                    break;
                n_blocks.y += 1;
            }
            n_blocks.y -= 1;
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_111(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[2],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));

            //get as many y threads as we can fit
            while (n_threads.x * n_threads.y <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
            {
                if (n_threads.y > CudaNdarray_HOST_DIMS(%(x)s)[1])
                    break;
                n_threads.y += 1;
            }
            n_threads.y -= 1;

            //get as many z threads as we can fit
            while (n_threads.x * n_threads.y * n_threads.z <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
            {
                if (n_threads.z > CudaNdarray_HOST_DIMS(%(x)s)[0])
                    break;
                n_threads.z += 1;
            }
            n_threads.z -= 1;

            dim3 n_blocks(1,1,1);
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_0011(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;

            dim3 n_blocks(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                        NUM_VECTOR_OP_BLOCKS));

            while (n_blocks.x * n_blocks.y <= NUM_VECTOR_OP_BLOCKS &&
                   n_blocks.y < CudaNdarray_HOST_DIMS(%(x)s)[1])
            {
                n_blocks.y += 1;
            }

            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[3],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));
            while (n_threads.x * n_threads.y <= NUM_VECTOR_OP_THREADS_PER_BLOCK
                   && n_threads.y < CudaNdarray_HOST_DIMS(%(x)s)[2]
                   && n_threads.x * n_threads.y * sizeof(%(dtype)s) <=
                      (15 * 1024 - 200))
            {
                n_threads.y += 1;
            }

            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_1111(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[2],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));

            //get as many y threads as we can fit
            while (n_threads.x * n_threads.y <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
            {
                if (n_threads.y > CudaNdarray_HOST_DIMS(%(x)s)[1])
                    break;
                n_threads.y += 1;
            }
            n_threads.y -= 1;

            //get as many z threads as we can fit
            while (n_threads.x * n_threads.y * n_threads.z <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
            {
                if (n_threads.z > CudaNdarray_HOST_DIMS(%(x)s)[0])
                    break;
                n_threads.z += 1;
            }
            n_threads.z -= 1;

            dim3 n_blocks(1,1,1);
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_reduce_1011(self, sio, node, name, x, z, fail):
        makecall = self._makecall(node, name, x, z, fail)
        print("""
        {
            int verbose = 0;
            dim3 n_threads(
                    std::min(CudaNdarray_HOST_DIMS(%(x)s)[3],
                            NUM_VECTOR_OP_THREADS_PER_BLOCK));

            while (n_threads.x * (n_threads.y+1) <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
                ++n_threads.y;
            if (n_threads.y > CudaNdarray_HOST_DIMS(%(x)s)[2])
                n_threads.y = CudaNdarray_HOST_DIMS(%(x)s)[2];

            while (n_threads.x * n_threads.y * (n_threads.z+1) <=
                   NUM_VECTOR_OP_THREADS_PER_BLOCK)
                ++n_threads.z;
            if (n_threads.z > 64)
                n_threads.z = 64;
            if (n_threads.z > CudaNdarray_HOST_DIMS(%(x)s)[0])
                n_threads.z = CudaNdarray_HOST_DIMS(%(x)s)[0];

            dim3 n_blocks(CudaNdarray_HOST_DIMS(%(x)s)[1]);
            %(makecall)s
        }
        """ % locals(), file=sio)

    def c_code_cache_version(self):
        return (21,)

    def c_support_code_apply(self, nodename, contig=False):
        sio = StringIO.StringIO()
        nd_in = len(self.reduce_mask)
        dtype = self.dtype
        if contig:  # all(i == 1 for i in self.reduce_mask):
            #this kernel is ok for up to a few thousand elements, but
            # it only runs on ONE multiprocessor
            reducebuf = self._k_reduce_buf('Z[0]')
            print("""
            __global__ void kernel_reduce_sum_ccontig_%(nodename)s(
                    const int d0,
                    const %(dtype)s *A,
                    %(dtype)s * Z)
            {
                const int threadCount = blockDim.x;
                const int threadNum = threadIdx.x;
                extern __shared__ %(dtype)s buf[];
                %(dtype)s mysum = 0.0f;

                if (warpSize != 32)
                {
                    return;  //TODO: set error code
                }

                for (int i0 = threadIdx.x; i0 < d0; i0 += blockDim.x)
                {
                    mysum += A[i0];
                }
                %(reducebuf)s
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (1,):
            #this kernel is ok for up to a few thousand elements, but
            # it only runs on ONE multiprocessor
            reducebuf = self._k_reduce_buf('Z[0]')
            decl = self._k_decl(nodename)
            print("""
            %(decl)s
            {
                const int threadCount = blockDim.x;
                const int threadNum = threadIdx.x;
                extern __shared__ %(dtype)s buf[];
                %(dtype)s mysum = 0.0f;

                if (warpSize != 32)
                {
                    return;  //TODO: set error code
                }

                for (int i0 = threadIdx.x; i0 < d0; i0 += blockDim.x)
                {
                    %(dtype)s Ai = A[i0 * sA0];
                    mysum += Ai;
                }
                %(reducebuf)s
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (1, 1):
            #this kernel is ok for up to a few thousand elements, but
            # it only runs on ONE multiprocessor
            reducebuf = self._k_reduce_buf('Z[0]')
            decl = self._k_decl(nodename)
            init = self._k_init(nodename)
            print(decl, file=sio)
            print(" { ", file=sio)
            print(init, file=sio)
            print("""
                for (int i0 = threadIdx.y; i0 < d0; i0 += blockDim.y)
                {
                    for (int i1 = threadIdx.x; i1 < d1; i1 += blockDim.x)
                    {
                        %(dtype)s Ai = A[i0 * sA0 + i1 * sA1];
                        mysum += Ai;
                    }
                }
            """ % locals(), file=sio)
            print(reducebuf, file=sio)
            print(" } ", file=sio)

        #01, 011, 0111
        if (0 == self.reduce_mask[0] and
            all(self.reduce_mask[1:]) and nd_in in[2, 3, 4]):
            # this kernel uses one block for each row.
            # threads per block for each element per row.

            N_pattern = ''.join(['1'] * (nd_in - 1))
            if nd_in == 2:
                for_i1 = "for(int i1 = threadIdx.x; i1 < d1; i1 += blockDim.x)"
                for_i2 = "int i2=0, sA2=0;"
                for_i3 = "int i3=0, sA3=0;"
            if nd_in == 3:
                for_i1 = "for(int i1 = threadIdx.y; i1 < d1; i1 += blockDim.y)"
                for_i2 = "for(int i2 = threadIdx.x; i2 < d2; i2 += blockDim.x)"
                for_i3 = "int i3=0, sA3=0;"
            if nd_in == 4:
                for_i1 = "for(int i1 = threadIdx.z; i1 < d1; i1 += blockDim.z)"
                for_i2 = "for(int i2 = threadIdx.y; i2 < d2; i2 += blockDim.y)"
                for_i3 = "for(int i3 = threadIdx.x; i3 < d3; i3 += blockDim.x)"

            reducebuf = self._k_reduce_buf('Z[i0 * sZ0]')
            param_dim = ",".join(["const int d%(i)s" % locals()
                                  for i in range(nd_in)])
            param_strides = ",".join(["const int sA%(i)s" % locals()
                                      for i in range(nd_in)])
            decl = self._k_decl(nodename)
            init = self._k_init(nodename)
            print("""
            %(decl)s{
                %(init)s
                for (int i0 = blockIdx.x; i0 < d0; i0 += gridDim.x){
                  mysum = 0;
                  %(for_i1)s{
                    %(for_i2)s{
                      %(for_i3)s{
                        %(dtype)s Ai = A[i3 * sA3 + i2 * sA2 +
                                     i1 * sA1 + i0 * sA0];
                        mysum += Ai;
                      }
                    }
                  }
                  %(reducebuf)s
                }
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (0, 1, 0) or self.reduce_mask == (1, 0):
            # this kernel uses one block for each column,
            # threads per block for each element per column.

            #TODO: This kernel is pretty inefficient in terms of
            #      reading, because if A is c_contiguous (typical
            #      case) then each warp is accessing non-contigous
            #      memory (a segment of a column).
            reducebuf = self._k_reduce_buf('Z[i0 * sZ0 + i2*sZ1]')
            print("""
            __global__ void kernel_reduce_sum_010_%(nodename)s(
                    const int d0,
                    const int d1,
                    const int d2,
                    const %(dtype)s *A, const int sA0,
                    const int sA1, const int sA2,
                    %(dtype)s * Z, const int sZ0, const int sZ1)
            {
                const int threadCount = blockDim.x;
                const int threadNum = threadIdx.x;
                extern __shared__ %(dtype)s buf[];

                if (warpSize != 32)
                {
                    return;  //TODO: set error code
                }


                for (int i0 = blockIdx.x; i0 < d0; i0 += gridDim.x)
                {
                    for (int i2 = blockIdx.y; i2 < d2; i2 += gridDim.y)
                    {
                        %(dtype)s mysum = 0.0f;
                        for (int i1 = threadIdx.x; i1 < d1; i1 += blockDim.x)
                        {
                            mysum += A[i0 * sA0 + i1 * sA1 + i2 * sA2];
                        }
                        %(reducebuf)s
                    }
                }

            }
            """ % locals(), file=sio)
        if self.reduce_mask == (0, 1, 0):
            print("""
            __global__ void kernel_reduce_sum_010_AD_%(nodename)s(
                    const int A,
                    const int B,
                    const int C,
                    const int D,
                    //const int E, // THIS is 32
                    const %(dtype)s *X, const int sX0,
                    const int sX1, const int sX2,
                    %(dtype)s * Z, const int sZ0, const int sZ1)
            {
                const int threadCount = blockDim.x;
                const int threadNum = threadIdx.x;
                %(dtype)s mysum = 0.0f;

                if (warpSize != 32)
                {
                    return;  //TODO: set error code
                }

                for (int a = blockIdx.x; a < A; a += gridDim.x)
                {
                    for (int i2_D = blockIdx.y; i2_D < D; i2_D += gridDim.y)
                    {
                        int c = i2_D * 32 + threadIdx.x;
                        if (c < C)
                        {
                            mysum = 0;
                            for (int b = 0; b < B; ++b)
                            {
                                mysum += X[a * sX0 + b * sX1 + c * sX2];
                            }
                            Z[a * sZ0 + c * sZ1] = mysum;
                        }
                    }
                }

            }
            """ % locals(), file=sio)
        if self.reduce_mask == (0, 1, 0):
            #
            # This kernel is optimized when the inner most dimensions
            # have the smallest stride.

            # this kernel uses one block for multiple column(up to 32TODO),
            # threads per block for each element per column.

#thread.x = dim 2 contiguous
#thread.y = dim 1
#block.x = dim 0
#block.y = dim 1 rest
            init = self._k_init(nodename)
            decl = self._k_decl(nodename, pattern="010_inner")
            reducebuf = self._k_reduce_buf_multiple('Z[i0 * sZ0 + i2*sZ1]',
                                                    'blockDim.x')
            reducebuf = self._k_reduce_buf_multiple('Z[i0 * sZ0 + i2*sZ1]',
                                                    'blockDim.x')
            print("""
            %(decl)s
            {
             if(warpSize<blockDim.x){
               //TODO: set error code
// need to be positive to work with unsigned
               Z[0] = 666;
               return;
              }

              %(init)s
              for (int i0 = blockIdx.x; i0 < d0; i0 += gridDim.x)
              {
                for (int i2 = blockIdx.y*blockDim.x+threadIdx.x;
                     i2 < d2; i2 += gridDim.y*blockDim.x)
                 {
                  for (int i1 = threadIdx.y; i1 < d1; i1 += blockDim.y)
                  {
                      mysum += A[i0 * sA0 + i1 * sA1 + i2 * sA2];
                  }
                  %(reducebuf)s
                 }
              }
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (1, 1, 0):
            # this kernel uses one block for each column,
            # threads per block for each element per column.

            #TODO: This kernel is pretty inefficient in terms of
            #      reading, because if A is c_contiguous (typical
            #      case) then each warp is accessing non-contigous
            #      memory (a segment of a column).
            reducebuf = self._k_reduce_buf('Z[blockIdx.x * sZ0]')
            print("""
            __global__ void kernel_reduce_sum_110_%(nodename)s(
                    const int d0,
                    const int d1,
                    const int d2,
                    const %(dtype)s *A, const int sA0,
                    const int sA1, const int sA2,
                    %(dtype)s * Z, const int sZ0)
            {
                const int threadCount = blockDim.x * blockDim.y;
                const int threadNum = threadIdx.y * blockDim.x + threadIdx.x;
                extern __shared__ %(dtype)s buf[];
                %(dtype)s mysum = 0.0f;

                if (warpSize != 32)
                {
                    //TODO: set error code
                    Z[blockIdx.x * sZ0] = 666;
                    return;
                }

                for (int i0 = threadIdx.y; i0 < d0; i0 += blockDim.y)
                {
                    for (int i1 = threadIdx.x; i1 < d1; i1 += blockDim.x)
                    {
                        %(dtype)s Ai = A[i0 * sA0 + i1 * sA1 +
                                         blockIdx.x * sA2];
                        mysum += Ai;
                    }
                }

                %(reducebuf)s
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (1, 0, 0):
            reducebuf = self._k_reduce_buf('Z[i1 * sZ0 + i2 * sZ1]')
            decl = self._k_decl(nodename)
            init = self._k_init(nodename)
            print("""
            %(decl)s
            {
                %(init)s
                for (int i2 = blockIdx.y; i2 < d2; i2 += gridDim.y)
                {
                    for (int i1 = blockIdx.x; i1 < d1; i1 += gridDim.x)
                    {
                        mysum = 0;
                        for (int i0 = threadIdx.x; i0 < d0; i0 += blockDim.x)
                        {
                            mysum += A[i0 * sA0 + i1 * sA1 + i2 * sA2];
                        }
                        %(reducebuf)s
                    }
                }
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (1, 1, 1):
            reducebuf = self._k_reduce_buf('Z[0]')
            decl = self._k_decl(nodename)
            init = self._k_init(nodename)
            print("""
            %(decl)s
            {
                %(init)s

                for (int i0 = threadIdx.z; i0 < d0; i0 += blockDim.z)
                {
                    for (int i1 = threadIdx.y; i1 < d1; i1 += blockDim.y)
                    {
                        for (int i2 = threadIdx.x; i2 < d2; i2 += blockDim.x)
                        {
                            mysum += A[i0 * sA0 + i1 * sA1 + i2 * sA2];
                        }
                    }
                }
""" % locals(), file=sio)
            print(reducebuf, "}", file=sio)

        if self.reduce_mask == (0, 0, 1):
            # this kernel uses one block for each row,
            # threads per block for each element per row.
            reducebuf = self._k_reduce_buf('Z[i0 * sZ0 + i1 * sZ1]')
            print("""
            __global__ void kernel_reduce_sum_001_%(nodename)s(
                    const int d0,
                    const int d1,
                    const int d2,
                    const %(dtype)s *A, const int sA0,
                    const int sA1, const int sA2,
                    %(dtype)s * Z, const int sZ0, const int sZ1)
            {
                const int threadCount = blockDim.x;
                const int threadNum = threadIdx.x;
                extern __shared__ %(dtype)s buf[];

                if (warpSize != 32)
                {
                    return;  //TODO: set error code
                }

                for (int i0 = blockIdx.x; i0 < d0; i0 += gridDim.x)
                {
                    for (int i1 = blockIdx.y; i1 < d1; i1 += gridDim.y)
                    {
                        %(dtype)s mysum = 0.0f;
                        for (int i2 = threadIdx.x; i2 < d2; i2 += blockDim.x)
                        {
                            mysum += A[i0 * sA0 + i1 * sA1 + i2 * sA2];
                        }
                        %(reducebuf)s
                    }
                }
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (0, 0, 1, 1):
            # this kernel uses one block for each row,
            # threads per block for each element per row.
            reducebuf = self._k_reduce_buf('Z[i0 * sZ0 + i1 * sZ1]')
            decl = self._k_decl(nodename)
            init = self._k_init(nodename)
            print("""
            %(decl)s
            {
                %(init)s

                for (int i0 = blockIdx.x; i0 < d0; i0 += gridDim.x)
                {
                    for (int i1 = blockIdx.y; i1 < d1; i1 += gridDim.y)
                    {
                        %(dtype)s mysum = 0.0f;
                    for (int i2 = threadIdx.y; i2 < d2; i2 += blockDim.y)
                    {
                        for (int i3 = threadIdx.x; i3 < d3; i3 += blockDim.x)
                        {
                            mysum += A[i0 * sA0 + i1 * sA1 +
                                       i2 * sA2 + i3 * sA3];
                        }
                    }
                        %(reducebuf)s
                    }
                }
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (0, 1, 0, 1):
            # this kernel uses one block for each row,
            # threads per block for each element per row.
            reducebuf = self._k_reduce_buf('Z[i0 * sZ0 + i2 * sZ1]')
            decl = self._k_decl(nodename)
            init = self._k_init(nodename)
            print("""
            %(decl)s
            {
                %(init)s

                for (int i0 = blockIdx.x; i0 < d0; i0 += gridDim.x)
                {
                    for (int i2 = blockIdx.y; i2 < d2; i2 += gridDim.y)
                    {
                        %(dtype)s mysum = 0.0f;
                    for (int i1 = threadIdx.y; i1 < d1; i1 += blockDim.y)
                    {
                        for (int i3 = threadIdx.x; i3 < d3; i3 += blockDim.x)
                        {
                            mysum += A[i0 * sA0 + i1 * sA1 +
                                       i2 * sA2 + i3 * sA3];
                        }
                    }
                        %(reducebuf)s
                    }
                }
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (1, 1, 1, 1):
            reducebuf = self._k_reduce_buf('Z[0]')
            decl = self._k_decl(nodename)
            init = self._k_init(nodename)
            print("""
            %(decl)s
            {
                %(init)s
                mysum = 0;
              for (int i0 = 0; i0 < d0; i0++)
                for (int i1 = threadIdx.z; i1 < d1; i1 += blockDim.z)
                {
                    for (int i2 = threadIdx.y; i2 < d2; i2 += blockDim.y)
                    {
                        for (int i3 = threadIdx.x; i3 < d3; i3 += blockDim.x)
                        {
                            mysum += A[i0 * sA0 + i1 * sA1 +
                                       i2 * sA2 + i3 * sA3];
                        }
                    }
                }
                %(reducebuf)s
            }
            """ % locals(), file=sio)
        if self.reduce_mask == (1, 0, 1, 1):
            reducebuf = self._k_reduce_buf('Z[blockIdx.x*sZ0]')
            print("""
            __global__ void kernel_reduce_sum_1011_%(nodename)s(
                    const int d0,
                    const int d1,
                    const int d2,
                    const int d3,
                    const %(dtype)s *A, const int sA0,
                    const int sA1, const int sA2, const int sA3,
                    %(dtype)s * Z, const int sZ0)
            {
                const int threadCount = blockDim.x * blockDim.y * blockDim.z;
                const int threadNum = threadIdx.z * blockDim.x * blockDim.y +
                                      threadIdx.y * blockDim.x + threadIdx.x;
                extern __shared__ %(dtype)s buf[];
                %(dtype)s mysum = 0.0f;

                if (warpSize != 32)
                {
                    return;  //TODO: set error code
                }

                for (int i0 = threadIdx.z; i0 < d0; i0 += blockDim.z)
                {
                    for (int i2 = threadIdx.y; i2 < d2; i2 += blockDim.y)
                    {
                        for (int i3 = threadIdx.x; i3 < d3; i3 += blockDim.x)
                        {
                            %(dtype)sy Ai = A[i0 * sA0 + blockIdx.x * sA1 +
                                         i2 * sA2 + i3 * sA3];
                            mysum += Ai;
                        }
                    }
                }
                %(reducebuf)s
            }
            """ % locals(), file=sio)
        return sio.getvalue()
