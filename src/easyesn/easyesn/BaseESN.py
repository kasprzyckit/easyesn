"""
    Basic implementation of an ESN.
"""

#from __future__ import absolute_import

import numpy as np
import numpy.random as rnd
import dill as pickle
import scipy as sp
import progressbar

#import backend as B

from . import backend as B

class BaseESN(object):
    def __init__(self, n_input, n_reservoir, n_output,
                 spectralRadius=1.0, noiseLevel=0.01, inputScaling=None,
                 leakingRate=1.0, feedbackScaling = 1.0, reservoirDensity=0.2, randomSeed=None,
                 out_activation=lambda x: x, out_inverse_activation=lambda x: x,
                 weightGeneration='naive', bias=1.0, outputBias=1.0, outputInputScaling=1.0,
                 feedback=False, input_density=1.0, activation = B.tanh, activationDerivation=lambda x: 1.0/B.cosh(x)**2):

        self.n_input = n_input
        self.n_reservoir = n_reservoir
        self.n_output = n_output

        self._spectralRadius = spectralRadius
        self._noiseLevel = noiseLevel
        self._reservoirDensity = reservoirDensity
        self._leakingRate = leakingRate
        self._feedbackScaling = feedbackScaling
        self.input_density = input_density
        self._activation = activation
        self._activationDerivation = activationDerivation

        if inputScaling is None:
            self._inputScaling = 1.0
        if np.isscalar(self._inputScaling):
            inputScaling = B.ones(n_input) * self._inputScaling
        else:
            if len(self._inputScaling) != self.n_input:
                raise ValueError("Dimension of inputScaling ({0}) does not match the input data dimension ({1})".format(len(self._inputScaling), n_input))
            self._inputScaling = inputScaling

        self._expandedInputScaling = B.vstack((1.0, inputScaling.reshape(-1,1))).flatten()

        self.out_activation = out_activation
        self.out_inverse_activation = out_inverse_activation

        if randomSeed is not None:
            rnd.seed(randomSeed)

        self._bias = bias
        self._outputBias = outputBias
        self._outputInputScaling = outputInputScaling
        self._createReservoir(weightGeneration, feedback)


    def setSpectralRadius(self, newSpectralRadius):
        self._W = self._W * ( newSpectralRadius / self._spectralRadius )
        self._spectralRadius = newSpectralRadius
        #TODO numerical instability

    def setLeakingRate(self, newLeakingRate):
        self._leakingRate = newLeakingRate

    def setInputScaling(self, newInputScaling):
        inputScaling = B.ones(self.n_input) * self._inputScaling
        self._expandedInputScaling = B.vstack((1.0, inputScaling.reshape(-1, 1))).flatten()
        self._WInput = self._WInput * ( self._expandedInputScaling / self._inputScaling )
        self._inputScaling = newInputScaling

    def setFeedbackScaling(self, newFeedbackScaling):
        self._WFeedback = self._WFeedback * ( newFeedbackScaling / self._feedbackScaling)
        self._feedbackScaling = newFeedbackScaling


    def resetState(self):
        self._x = B.zeros_like(self._x)

    def propagate(self, inputData, outputData=None, transientTime=0, verbose=0, x=None, steps="auto"):
        if x is None:
            x = self._x

        inputLength = steps
        if inputData is None:
            if outputData is not None: 
                inputLength = len(outputData)
        else:
            inputLength = len(inputData)
        if inputLength == "auto":
            raise ValueError("inputData and outputData are both None. Therefore, steps must not be `auto`.")

        # define states' matrix
        X = B.zeros((1 + self.n_input + self.n_reservoir, inputLength - transientTime))

        if (verbose > 0):
            bar = progressbar.ProgressBar(max_value=inputLength, redirect_stdout=True, poll_interval=0.0001)
            bar.update(0)

        if self._WFeedback is None:
            #do not distinguish between whether inputData is None or not, as the feedback has been disabled
            #therefore, the input has to be anything but None

            for t in range(inputLength):
                u = self.update(inputData[t], x=x)
                if (t >= transientTime):
                    #add valueset to the states' matrix
                    X[:,t-transientTime] = B.vstack((self._outputBias, self._outputInputScaling*u, x))[:,0]
                if (verbose > 0):
                    bar.update(t)
        else:
            if outputData is None:
                Y = B.empty((inputLength-transientTime, self.n_output))

            previousOutputData = B.zeros((1, self.n_output))

            if inputData is None:
                for t in range(inputLength):
                    self.update(None, previousOutputData, x=x)
                    if (t >= transientTime):
                        #add valueset to the states' matrix
                        X[:,t-transientTime] = B.vstack((self._outputBias, x))[:,0]
                    if outputData is None:
                        #calculate the prediction using the trained model
                        if (self._solver in ["sklearn_auto", "sklearn_lsqr", "sklearn_sag", "sklearn_svd"]):
                            previousOutputData = self._ridgeSolver.predict(B.vstack((self._outputBias, self._x)).T)
                        else:
                            previousOutputData = B.dot(self._WOut, B.vstack((self._outputBias, self._x)))
                        if t >= transientTime:
                            Y[t-transientTime, :] = previousOutputData
                    else:
                        previousOutputData = outputData[t]
                
                    if (verbose > 0):
                        bar.update(t)
            else:
                for t in range(inputLength):
                    u = self.update(inputData[t], previousOutputData, x=x)
                    if (t >= transientTime):
                        #add valueset to the states' matrix
                        X[:,t-transientTime] = B.vstack((self._outputBias, self._outputInputScaling*u, x))[:,0]
                    if outputData is None:
                        #calculate the prediction using the trained model
                        if (self._solver in ["sklearn_auto", "sklearn_lsqr", "sklearn_sag", "sklearn_svd"]):
                            previousOutputData = self._ridgeSolver.predict(B.vstack((self._outputBias, self._outputInputScaling*u, self._x)).T)
                        else:
                            previousOutputData = B.dot(self._WOut, B.vstack((self._outputBias, self._outputInputScaling*u, self._x)))
                        Y[t, :] = previousOutputData
                    else:
                        previousOutputData = outputData[t]
                
                    if (verbose > 0):
                        bar.update(t)
                                 
        if (verbose > 0):
            bar.finish()

        if self._WFeedback is not None and outputData is None:
            return X, Y
        else:
            return X

    """
        Generates a random rotation matrix, used in the SORM initilization (see http://ftp.math.uni-rostock.de/pub/preprint/2012/pre12_01.pdf)
    """
    def create_random_rotation_matrix(self):
        h = rnd.randint(low=0, high=self.n_reservoir)
        k = rnd.randint(low=0, high=self.n_reservoir)

        phi = rnd.rand(1)*2*np.pi

        Q = B.identity(self.n_reservoir)
        Q[h, h] = np.cos(phi)
        Q[k, k] = np.cos(phi)

        Q[h, k] = -np.sin(phi)
        Q[k, h] = np.sin(phi)

        return Q

    """
        Internal method to create the matrices W_in, W and W_fb of the ESN
    """
    def _createReservoir(self, weightGeneration, feedback=False, verbose=False):
        #naive generation of the matrix W by using random weights
        if weightGeneration == 'naive':
            #random weight matrix from -0.5 to 0.5
            self._W = rnd.rand(self.n_reservoir, self.n_reservoir) - 0.5

            #set sparseness% to zero
            mask = rnd.rand(self.n_reservoir, self.n_reservoir) > self._reservoirDensity
            self._W[mask] = 0.0

            _W_eigenvalues = B.abs(np.linalg.eig(self._W)[0])
            self._W *= self._spectralRadius / B.max(_W_eigenvalues)

        #generation using the SORM technique (see http://ftp.math.uni-rostock.de/pub/preprint/2012/pre12_01.pdf)
        elif weightGeneration == "SORM":
            self._W = np.identity(self.n_reservoir)

            number_nonzero_elements = self._reservoirDensity * self.n_reservoir * self.n_reservoir
            i = 0

            while np.count_nonzero(self._W) < number_nonzero_elements:
                i += 1
                Q = self.create_random_rotation_matrix()
                self._W = Q.dot(self._W)
            
            self._W *= self._spectralRadius

        #generation using the proposed method of Yildiz
        elif weightGeneration == 'advanced':
            #two create W we must follow some steps:
            #at first, create a W = |W|
            #make it sparse
            #then scale its spectral radius to rho(W) = 1 (according to Yildiz with x(n+1) = (1-a)*x(n)+a*f(...))
            #then change randomly the signs of the matrix

            #random weight matrix from 0 to 0.5

            self._W = rnd.rand(self.n_reservoir, self.n_reservoir) / 2

            #set sparseness% to zero
            mask = B.rand(self.n_reservoir, self.n_reservoir) > self._reservoirDensity
            self._W[mask] = 0.0

            from scipy.sparse.linalg.eigen.arpack.arpack import ArpackNoConvergence
            #just calculate the largest EV - hopefully this is the right code to do so...
            try:
                #this is just a good approximation, so this code might fail
                _W_eigenvalue = B.max(np.abs(sp.sparse.linalg.eigs(self._W, k=1)[0]))
            except ArpackNoConvergence:
                #this is the safe fall back method to calculate the EV
                _W_eigenvalue = B.max(B.abs(sp.linalg.eigvals(self._W)))
            #_W_eigenvalue = B.max(B.abs(np.linalg.eig(self._W)[0]))

            self._W *= self._spectralRadius / _W_eigenvalue

            if verbose:
                M = self._leakingRate*self._W + (1 - self._leakingRate)*np.identity(n=self._W.shape[0])
                M_eigenvalue = B.max(B.abs(np.linalg.eig(M)[0]))#np.max(np.abs(sp.sparse.linalg.eigs(M, k=1)[0]))
                print("eff. spectral radius: {0}".format(M_eigenvalue))

            #change random signs
            random_signs = B.power(-1, rnd.random_integers(self.n_reservoir, self.n_reservoir))

            self._W = B.multiply(self._W, random_signs)
        elif weightGeneration == 'custom':
            pass
        else:
            raise ValueError("The weightGeneration property must be one of the following values: naive, advanced, SORM, custom")

        #check of the user is really using one of the internal methods, or wants to create W by his own
        if (weightGeneration != 'custom'):
            #random weight matrix for the input from -0.5 to 0.5
            self._WInput = B.rand(self.n_reservoir, 1 + self.n_input)-0.5

            #scale the input_density to prevent saturated reservoir nodes
            if (self.input_density != 1.0):
                #make the input matrix as dense as requested
                input_topology = (np.ones_like(self._WInput) == 1.0)
                nb_non_zero_input = int(self.input_density * self.n_input)
                for n in range(self.n_reservoir):
                    input_topology[n][rnd.permutation(np.arange(1+self.n_input))[:nb_non_zero_input]] = False

                self._WInput[input_topology] = 0.0

            self._WInput = self._WInput * self._expandedInputScaling

        #create the optional feedback matrix
        if feedback:
            self._WFeedback = B.rand(self.n_reservoir, 1 + self.n_output) - 0.5
            self._WFeedback *= self._feedbackScaling
        else:
            self._WFeedback = None


    def calculateLinearNetworkTransmissions(self, u, x=None):
        if x is None:
            x = self._x

        return B.dot(self._WInput, B.vstack((self._bias, u))) + B.dot(self._W, x)

    """
        Updates the inner states. Returns the UNSCALED but reshaped input of this step.
    """
    def update(self, inputData, outputData=None, x=None):
        if x is None:
            x = self._x

        if self._WFeedback is None:
            #reshape the data
            u = inputData.reshape(self.n_input, 1)

            #update the states
            transmission = self.calculateLinearNetworkTransmissions(u, x)
            x *= (1.0-self._leakingRate)
            x += self._leakingRate * self._activation(transmission + (B.rand()-0.5)*self._noiseLevel)
        
            return u

        else:
            #the input is allowed to be "empty" (size=0)
            if self.n_input != 0:
                #reshape the data
                u = inputData.reshape(self.n_input, 1)
                outputData = outputData.reshape(self.n_output, 1)

                #update the states
                transmission = self.calculateLinearNetworkTransmissions(u, x)
                x *= (1.0-self._leakingRate)
                x += self._leakingRate*self._activation(transmission +
                     B.dot(self._WFeedback, B.vstack((self._outputBias, outputData))) + (B.rand()-0.5)*self._noiseLevel)

                return u
            else:
                #reshape the data
                outputData = outputData.reshape(self.n_output, 1)
                #update the states
                transmission = B.dot(self._W, x)
                x *= (1.0-self._leakingRate)
                x += self._leakingRate*self._activation(transmission + B.dot(self._WFeedback, B.vstack((self._outputBias, outputData))) +
                     (B.rand()-0.5)*self._noiseLevel)

                return np.empty((0, 1))



    def isEpsilonClose(self, x, epsilon):
        for i in range(x.shape[0]):
            for j in range(x.shape[0]):
                if not B.all( B.abs( x[i] - x[j] ) < epsilon ):
                    return False

        return True


    def calculateTransientTime(self, inputs, epsilon, proximityLength = 50):
        x = B.empty((2, self.n_reservoir, 1))
        x[0] = B.zeros((self.n_reservoir, 1))
        x[1] = B.ones((self.n_reservoir, 1))

        return self._calculateTransientTime(x, inputs, epsilon, proximityLength)

    def _calculateTransientTime(self, x, inputs, outputs, epsilon, proximityLength = 50):
        c = 0
        length = inputs.shape[0] if inputs is not None else outputs.shape[0]
        for t in range(length):
            if self.isEpsilonClose(x, epsilon):
                if c >= proximityLength:
                    return t - proximityLength
                else:
                    c = c + 1
            else:
                c = 0

            u = inputs[t].reshape(-1, 1) if inputs is not None else None
            o = outputs[t].reshape(-1, 1) if inputs is not None else None
            for x_i in x:
                x_i = self.update(u, o, x_i)

    def getStateAtGivenPoint(self, inputs, outputs, point):
        x = B.zeros((self._noiseLevel, 1))

        length = inputs.shape[0] if inputs is not None else outputs.shape[0]
        for t in range(length):
            u = inputs[t].reshape(-1, 1) if inputs is not None else None
            o = outputs[t].reshape(-1, 1) if inputs is not None else None
            self.update(u, o, x)
            if t == point:
                return x

    def estimated_autocorrelation(self, x):
        n = x.shape[0]
        variance = B.var(x)
        x = x - B.mean(x)
        r = B.correlate(x, x, mode='full')[-n:]
        assert np.allclose(r, np.array([(x[:n - k] * x[-(n - k):]).sum() for k in range(n)]))
        result = r / (variance * (np.arange(n, 0, -1)))
        return result

    def SWD(self, series, intervall):
        differences = np.zeros(series.shape[0] - 2 * intervall)
        reference_series = series[:intervall]
        for i in range(intervall, series.shape[0] - intervall):
            differences[i - intervall] = int(np.sum(np.abs(reference_series - series[i:i + intervall])))

        return np.argmin(differences) + intervall, differences



    def reduceTransientTime(self):
        pass



    """
        Saves the ESN by pickling it.
    """
    def save(self, path):
        f = open(path, "wb")
        pickle.dump(self, f)
        f.close()

    """
        Loads a previously pickled ESN.
    """
    def load(path):
        f = open(path, "rb")
        result = pickle.load(f)
        f.close()
        return result
